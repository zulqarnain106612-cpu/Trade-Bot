"""
INV-032 / INV-033 -- the four observability producers, and the law that every
declared topic has one.

These four are the ones an operator cannot see any other way once the polls
are gone:

  drift       feature drift and the model-degradation verdict. Both are
              states, not streams, so both publish on transition -- and the
              recovery transition matters as much as the onset, because it is
              the only thing that says an incident is over.
  health      the runtime monitor's probe snapshot. Published every cycle
              rather than on change, because "still ok" and "the monitor died
              while saying ok" are the two states this has to separate.
  selftuning  a promotion or a rollback moves a live parameter.
  intel       a provider's circuit breaker opening. The intelligence layer
              fails open, so this transition is the *only* evidence that the
              feature pipeline has quietly started running on fallbacks.

INV-033 is the structural half: a topic nobody publishes is a panel that
renders once on mount and then stays wrong forever, and nothing about that
looks like a failure from either end.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from src.diagnostics.runtime_monitor import RuntimeMonitor
from src.diagnostics.signal_debugger import FeatureDriftMonitor, ModelDegradationTracker
from src.eventbus import TOPICS, EventBus
from src.intelligence.onchain.base import CircuitBreaker, _CBState
from src.tuning.audit import TuningAuditLog, TuningEventType

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# A spread wide enough that a shift of +20 is unambiguous drift (ks ~= 1.15
# against a std of ~17.3, over a threshold of 0.19) and a shift of 0 is
# unambiguously not. Sixty samples with a window of sixty means one refill
# replaces the buffer outright, so a test can move a feature into drift and
# back out without the two populations mixing.
_BASELINE = [float(i) for i in range(60)]
_DRIFTED = [v + 20.0 for v in _BASELINE]


def _bus_for(monkeypatch: pytest.MonkeyPatch, module: str) -> EventBus:
    """A private bus in place of the process-wide one, per REG-0005."""
    bus = EventBus()
    monkeypatch.setattr(f"{module}.get_event_bus", lambda: bus)
    return bus


@pytest.fixture
def drift_bus(monkeypatch: pytest.MonkeyPatch) -> EventBus:
    return _bus_for(monkeypatch, "src.diagnostics.signal_debugger")


@pytest.fixture
def health_bus(monkeypatch: pytest.MonkeyPatch) -> EventBus:
    return _bus_for(monkeypatch, "src.diagnostics.runtime_monitor")


@pytest.fixture
def tuning_bus(monkeypatch: pytest.MonkeyPatch) -> EventBus:
    return _bus_for(monkeypatch, "src.tuning.audit")


@pytest.fixture
def intel_bus(monkeypatch: pytest.MonkeyPatch) -> EventBus:
    return _bus_for(monkeypatch, "src.intelligence.onchain.base")


def _drift_monitor(values: list[float]) -> FeatureDriftMonitor:
    monitor = FeatureDriftMonitor(window=len(_BASELINE))
    monitor.set_baseline("rsi_14", _BASELINE)
    for value in values:
        monitor.push("rsi_14", value)
    return monitor


class TestFeatureDrift:
    def test_a_feature_going_out_of_distribution_publishes(self, drift_bus: EventBus) -> None:
        sub = drift_bus.subscribe(["drift"])
        monitor = _drift_monitor(_DRIFTED)

        monitor.publish_if_changed()

        assert len(sub._queue) == 1
        data = sub._queue[0].data
        assert data["kind"] == "feature_drift"
        assert data["drifted_features"] == ["rsi_14"]
        assert data["feature_drift"][0]["ks_statistic"] > 0.19

    def test_a_feature_still_drifted_publishes_nothing(self, drift_bus: EventBus) -> None:
        sub = drift_bus.subscribe(["drift"])
        monitor = _drift_monitor(_DRIFTED)
        monitor.publish_if_changed()

        # Same buffer, same verdict, one more bar. A push per bar that
        # re-announced an unchanged 40-feature payload is the polling this
        # replaced, wearing a different hat.
        monitor.push("rsi_14", _DRIFTED[-1])
        monitor.publish_if_changed()

        assert len(sub._queue) == 1

    def test_drift_clearing_publishes_the_recovery(self, drift_bus: EventBus) -> None:
        sub = drift_bus.subscribe(["drift"])
        monitor = _drift_monitor(_DRIFTED)
        monitor.publish_if_changed()

        for value in _BASELINE:  # a full window back in distribution
            monitor.push("rsi_14", value)
        monitor.publish_if_changed()

        assert len(sub._queue) == 2
        assert sub._queue[1].data["drifted_features"] == []

    def test_a_healthy_cold_start_publishes_nothing(self, drift_bus: EventBus) -> None:
        sub = drift_bus.subscribe(["drift"])
        monitor = _drift_monitor(_BASELINE)

        monitor.publish_if_changed()

        assert len(sub._queue) == 0

    def test_check_all_still_reports_and_does_not_publish(self, drift_bus: EventBus) -> None:
        """
        The endpoint's entry point keeps its logging and gains no publish.

        Splitting the arithmetic out of it was only worth doing if the two
        callers stayed distinguishable: /debug/drift logs a line per drifted
        feature per request, and the tick path must not.
        """
        sub = drift_bus.subscribe(["drift"])
        monitor = _drift_monitor(_DRIFTED)

        records = monitor.check_all()

        assert [r.feature for r in records if r.drifted] == ["rsi_14"]
        assert len(sub._queue) == 0

    def test_check_all_on_a_clean_feature_reports_it_undrifted(self) -> None:
        monitor = _drift_monitor(_BASELINE)

        records = monitor.check_all()

        assert len(records) == 1
        assert records[0].drifted is False


def _degraded_tracker() -> ModelDegradationTracker:
    """Twenty resolved predictions, every one wrong: live accuracy 0.0."""
    tracker = ModelDegradationTracker(window=50)
    tracker.set_training_metrics(accuracy=0.80, f1=0.78)
    for _ in range(20):
        tracker.record_prediction(p_long=0.9, p_bet=0.9)
        tracker.resolve_last(actual_direction=0)
    return tracker


class TestModelDegradation:
    def test_crossing_into_degraded_publishes_the_verdict(self, drift_bus: EventBus) -> None:
        sub = drift_bus.subscribe(["drift"])
        tracker = _degraded_tracker()

        tracker.publish_if_changed()

        assert len(sub._queue) == 1
        data = sub._queue[0].data
        assert data["kind"] == "model_degradation"
        assert data["degraded"] is True
        assert data["retrain_recommended"] is True
        assert data["live_accuracy"] == pytest.approx(0.0)

    def test_a_verdict_that_has_not_flipped_publishes_nothing(self, drift_bus: EventBus) -> None:
        sub = drift_bus.subscribe(["drift"])
        tracker = _degraded_tracker()
        tracker.publish_if_changed()

        # live_accuracy moves on every resolve; the verdict does not.
        tracker.record_prediction(p_long=0.9, p_bet=0.9)
        tracker.resolve_last(actual_direction=0)
        tracker.publish_if_changed()

        assert len(sub._queue) == 1

    def test_a_healthy_model_publishes_nothing(self, drift_bus: EventBus) -> None:
        sub = drift_bus.subscribe(["drift"])
        tracker = ModelDegradationTracker(window=50)
        tracker.set_training_metrics(accuracy=0.55, f1=0.54)
        for _ in range(20):
            tracker.record_prediction(p_long=0.9, p_bet=0.9)
            tracker.resolve_last(actual_direction=1)

        tracker.publish_if_changed()

        assert len(sub._queue) == 0

    def test_check_degradation_still_reports_and_does_not_publish(
        self, drift_bus: EventBus
    ) -> None:
        sub = drift_bus.subscribe(["drift"])
        tracker = _degraded_tracker()

        report = tracker.check_degradation()

        assert report["degraded"] is True
        assert len(sub._queue) == 0

    def test_check_degradation_on_a_healthy_model_reports_it_healthy(self) -> None:
        tracker = ModelDegradationTracker(window=50)

        assert tracker.check_degradation()["degraded"] is False


class TestRuntimeHealth:
    async def test_a_completed_probe_cycle_publishes(self, health_bus: EventBus) -> None:
        sub = health_bus.subscribe(["health"])
        monitor = RuntimeMonitor()

        await monitor._run_all_probes()

        assert len(sub._queue) == 1
        data = sub._queue[0].data
        assert data["overall"] == "ok"
        # The whole snapshot, not a verdict: the panel that replaces the
        # /debug/health poll has no other source for the probe rows. Memory is
        # synthesized by the cycle itself, so it is there with nothing else
        # registered.
        assert "memory_rss_mb" in {probe["name"] for probe in data["probes"]}

    async def test_every_cycle_publishes_not_only_the_changed_ones(
        self, health_bus: EventBus
    ) -> None:
        """
        The liveness half. An operator has to be able to tell a monitor that
        keeps saying ok from one that died while saying ok, and the only
        difference between those is whether the frames keep arriving.
        """
        sub = health_bus.subscribe(["health"])
        monitor = RuntimeMonitor()

        await monitor._run_all_probes()
        await monitor._run_all_probes()

        assert len(sub._queue) == 2


class TestSelfTuning:
    def test_recording_a_promotion_publishes_it(
        self, tuning_bus: EventBus, tmp_path: pathlib.Path
    ) -> None:
        sub = tuning_bus.subscribe(["selftuning"])
        audit = TuningAuditLog(tmp_path / "self_tuning_audit.jsonl")

        audit.record("kelly_cap", TuningEventType.PROMOTED, {"from": 0.20, "to": 0.25})

        assert len(sub._queue) == 1
        data = sub._queue[0].data
        assert data["param_name"] == "kelly_cap"
        assert data["event_type"] == "promoted"
        assert data["details"] == {"from": 0.20, "to": 0.25}

    def test_the_event_reaches_the_log_before_the_bus(
        self, tuning_bus: EventBus, tmp_path: pathlib.Path
    ) -> None:
        """
        A dashboard that has seen an event the audit log has not is a
        discrepancy an incident review cannot account for, so the file write
        happens first and the publish cannot skip it.
        """
        path = tmp_path / "self_tuning_audit.jsonl"
        audit = TuningAuditLog(path)
        seen: list[int] = []
        tuning_bus.subscribe(["selftuning"])
        original = tuning_bus.publish

        def spy(topic: str, payload: object, **kwargs: object) -> None:
            seen.append(len(path.read_text().splitlines()))
            original(topic, payload, **kwargs)  # type: ignore[arg-type]

        tuning_bus.publish = spy  # type: ignore[method-assign]
        audit.record("kelly_cap", TuningEventType.ROLLED_BACK)

        assert seen == [1]

    @pytest.mark.parametrize(
        "event_type",
        [TuningEventType.PROMOTED, TuningEventType.ROLLED_BACK, TuningEventType.PAUSED],
    )
    def test_every_event_type_goes_out(
        self, tuning_bus: EventBus, tmp_path: pathlib.Path, event_type: TuningEventType
    ) -> None:
        sub = tuning_bus.subscribe(["selftuning"])
        audit = TuningAuditLog(tmp_path / "self_tuning_audit.jsonl")

        audit.record("kelly_cap", event_type)

        assert sub._queue[0].data["event_type"] == event_type.value


async def _boom() -> None:
    raise RuntimeError("provider unreachable")


async def _fine() -> str:
    return "ok"


class TestIntelProviderHealth:
    async def test_the_breaker_opening_publishes_the_provider(self, intel_bus: EventBus) -> None:
        sub = intel_bus.subscribe(["intel"])
        breaker = CircuitBreaker(failure_threshold=2, cooldown_s=0.0, name="ArkhamProvider")

        for _ in range(2):
            with pytest.raises(RuntimeError):
                await breaker.call(_boom)

        assert len(sub._queue) == 1
        data = sub._queue[0].data
        assert data["provider"] == "ArkhamProvider"
        assert data["state"] == "open"
        assert data["previous_state"] == "closed"
        assert data["reason"] == "threshold_reached"
        assert data["consecutive_failures"] == 2

    async def test_a_failure_below_the_threshold_publishes_nothing(
        self, intel_bus: EventBus
    ) -> None:
        sub = intel_bus.subscribe(["intel"])
        breaker = CircuitBreaker(failure_threshold=2, cooldown_s=0.0, name="DuneProvider")

        with pytest.raises(RuntimeError):
            await breaker.call(_boom)

        assert len(sub._queue) == 0

    async def test_a_success_while_closed_publishes_nothing(self, intel_bus: EventBus) -> None:
        sub = intel_bus.subscribe(["intel"])
        breaker = CircuitBreaker(name="DeFiLlamaProvider")

        assert await breaker.call(_fine) == "ok"

        assert len(sub._queue) == 0

    async def test_recovery_publishes_both_steps(self, intel_bus: EventBus) -> None:
        """
        A provider coming back is two transitions, and the operator needs
        both: half-open says the cooldown elapsed and a probe is in flight,
        closed says the probe worked. Collapsing them loses the distinction
        between recovering and recovered.
        """
        sub = intel_bus.subscribe(["intel"])
        breaker = CircuitBreaker(failure_threshold=1, cooldown_s=0.0, name="CoinglassProvider")
        with pytest.raises(RuntimeError):
            await breaker.call(_boom)
        sub._queue.clear()

        assert await breaker.call(_fine) == "ok"

        assert [e.data["state"] for e in sub._queue] == ["half_open", "closed"]
        assert [e.data["reason"] for e in sub._queue] == ["cooldown_elapsed", "probe_succeeded"]

    async def test_a_failed_probe_reopens(self, intel_bus: EventBus) -> None:
        sub = intel_bus.subscribe(["intel"])
        breaker = CircuitBreaker(failure_threshold=1, cooldown_s=0.0, name="CryptoQuantProvider")
        with pytest.raises(RuntimeError):
            await breaker.call(_boom)
        sub._queue.clear()

        with pytest.raises(RuntimeError):
            await breaker.call(_boom)

        assert [e.data["state"] for e in sub._queue] == ["half_open", "open"]
        assert sub._queue[1].data["reason"] == "probe_failed"

    def test_a_transition_to_the_current_state_is_a_no_op(self, intel_bus: EventBus) -> None:
        """
        The guard that makes _transition idempotent. Unreachable from call()
        today; it is there so the next path added -- a manual reset, a second
        failure mode -- cannot announce a change that did not happen.
        """
        sub = intel_bus.subscribe(["intel"])
        breaker = CircuitBreaker(name="ArkhamProvider")

        breaker._transition(_CBState.CLOSED, "already_closed")

        assert len(sub._queue) == 0

    def test_a_provider_names_its_own_breaker(self) -> None:
        from src.intelligence.onchain.defillama_provider import DeFiLlamaProvider

        assert DeFiLlamaProvider()._breaker._name == "DeFiLlamaProvider"


def _published_topics() -> set[str]:
    """
    Every literal topic published anywhere in src/, read from the syntax tree.

    AST rather than a regex because both call shapes are in the tree already
    -- ``get_event_bus().publish("regime", ...)`` and a hoisted
    ``bus = get_event_bus()`` followed by ``bus.publish("book", ...)`` -- and
    a regex that handles both also matches ``def publish`` and the unrelated
    publish methods in src/data/feeds.py. Restricting the scan to modules
    that import the bus is what keeps those out.
    """
    topics: set[str] = set()
    for path in sorted((REPO_ROOT / "src").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "get_event_bus" not in source:
            continue
        for node in ast.walk(ast.parse(source, filename=str(path))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "publish"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                topics.add(node.args[0].value)
    return topics


class TestEveryTopicHasAProducer:
    """INV-033."""

    def test_no_topic_is_declared_without_a_producer(self) -> None:
        """
        A topic in TOPICS that nothing publishes is the failure this whole
        effort is about, inverted: the client subscribes, the server accepts
        the subscription, the panel hydrates once from its endpoint and then
        never updates again. Nothing errors, nothing logs, and the panel is
        confidently wrong for as long as the process runs.
        """
        assert TOPICS - _published_topics() == set()

    def test_nothing_publishes_a_topic_the_bus_does_not_declare(self) -> None:
        """
        The other direction. publish() refuses an unknown topic by counting
        and logging rather than raising -- deliberately, so a typo in a risk
        gate cannot take the gate down -- which means a misspelled topic is
        invisible at runtime and has to be caught here instead.
        """
        assert _published_topics() - TOPICS == set()
