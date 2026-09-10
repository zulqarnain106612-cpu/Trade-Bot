"""
Strategy portfolio evaluation — the missing execution half of the registry.

src/strategies/registry.py defines the ``StrategyProtocol`` contract,
src/strategies/bootstrap.py registers every enabled family into the
process-wide registry, src/strategies/capital_allocator.py splits capital
across them and src/risk/strategy_kill_switch.py can disable them — but
nothing in the process ever called ``generate_signal()``. The portfolio was
registered, weighted, attributed and kill-switchable while being, at
runtime, permanently silent: the only signal that reached the executor was
the incumbent ``signal_engine_v1``, whose adapter is fed a precomputed
``SignalResult`` rather than polled.

This module is that missing half. It does three things and nothing more:

1. **Builds each strategy the context it declares.** Every family takes a
   different bar-equivalent context (``BreakoutContext``, ``FundingContext``,
   ...), so a single ``bar`` object cannot serve them all. A context builder
   per ``strategy_id`` maps this tick's market state onto the right type, and
   returns ``None`` when the data a family needs is not available.

2. **Makes silence explicit.** A family with no context *abstains* — a
   first-class, logged outcome with a reason — rather than being silently
   absent. That distinction is the whole point: the reason this layer did
   not exist for so long is that "registered but never asked" and "asked and
   flat" were indistinguishable from outside.

3. **Resolves disagreement instead of averaging it away.** Votes go through
   ``HorizonConflictResolver``, which reports an agreement ratio and flags a
   conflicted book. A conflicted portfolio is reported as conflicted, not
   quietly netted to a small position.

Two invariants carried from the domain priors:

* ``Signal.regime_fit == 0`` is documented as a *hard gate* ("do not trade in
  this regime"), not a scalar to multiply by. Such a signal is dropped from
  the vote entirely rather than down-weighted to near-zero, because a
  near-zero weight still lets a large enough confidence leak through.
* Allocation weights bias the vote but never create one: a strategy with
  zero confidence contributes nothing regardless of how much capital it
  holds, since the resolver multiplies weight by confidence.

Evaluation is deliberately **advisory**. It computes and reports a portfolio
direction; it does not route orders. Sizing stays with Kelly and the risk
gates, which are the only components authorised to turn conviction into
notional. Wiring an advisory layer in first is what makes the out-of-sample
requirement in CLAUDE.md enforceable — the portfolio's opinion is recorded
tick by tick before any capital follows it.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

import numpy as np
import pandas as pd
import structlog

from src.risk.conflict_resolver import ConflictResolution, HorizonConflictResolver
from src.strategies.basis_trade import BasisTradeContext
from src.strategies.breakout import BreakoutContext
from src.strategies.cross_exchange_arb import CrossExchangeContext
from src.strategies.funding_carry import FundingContext
from src.strategies.mean_reversion import PairContext
from src.strategies.registry import Signal, StrategyProtocol, StrategyRegistry, get_default_registry
from src.strategies.signal_engine_adapter import STRATEGY_ID_SIGNAL_ENGINE
from src.strategies.xsec_momentum import UniverseContext

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


# Sentinel context for result-driven strategies (the SignalEngine adapter),
# which ignore the `bar` argument entirely. It is not `None`, because `None`
# is the builders' "I have no data" answer and would abstain the incumbent.
NO_CONTEXT: object = object()

# Minimum bars a range-breakout context is worth constructing from. Below
# this the strategy answers flat by its own guard anyway; abstaining instead
# keeps "not enough history" out of the vote as a reason rather than as a
# zero-confidence vote indistinguishable from "looked, saw nothing".
_MIN_BREAKOUT_BARS: int = 60

# A funding z-score needs enough observations to mean anything. Eight-hour
# funding means 3 points/day, so 24 samples is ~8 days of history.
_MIN_FUNDING_SAMPLES: int = 24

# Maximum wall-clock gap between two venues' quotes before the pair is
# treated as unusable. The cross-exchange family enters on 15 bps, and
# crypto routinely moves that far in a second, so a wider window would let
# fetch latency masquerade as a tradeable basis.
_MAX_VENUE_QUOTE_SKEW_S: float = 2.0


class VerdictStatus(StrEnum):
    """Why a strategy did or did not contribute a vote this tick."""

    SIGNAL = "signal"
    """Polled, returned a tradeable-shaped Signal that joined the vote."""

    FLAT = "flat"
    """Polled, returned direction 0 — an opinion, just not a directional one."""

    ABSTAINED = "abstained"
    """Not polled: no context could be built from the available data."""

    REGIME_GATED = "regime_gated"
    """Polled, but returned regime_fit == 0 — a hard "not in this regime"."""

    DISABLED = "disabled"
    """Registered but switched off by the kill switch / enabled set."""

    ERROR = "error"
    """Raised. Recorded, never propagated into the trade path."""


@dataclass(frozen=True, slots=True)
class StrategyVerdict:
    """One strategy's outcome for one evaluation."""

    strategy_id: str
    status: VerdictStatus
    signal: Signal | None = None
    weight: float = 0.0
    reason: str = ""

    @property
    def votes(self) -> bool:
        """True when this verdict carries a directional vote."""
        return self.status is VerdictStatus.SIGNAL

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "status": self.status.value,
            "direction": self.signal.direction if self.signal else 0,
            "confidence": round(self.signal.confidence, 6) if self.signal else 0.0,
            "regime_fit": round(self.signal.regime_fit, 6) if self.signal else 0.0,
            "weight": round(self.weight, 6),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class PortfolioEvaluation:
    """The portfolio's aggregate opinion, plus every input that formed it."""

    verdicts: tuple[StrategyVerdict, ...]
    resolution: ConflictResolution

    @property
    def direction(self) -> int:
        """-1/0/+1 portfolio direction, 0 when the book is conflicted."""
        # A conflicted book has no direction worth acting on. Reporting the
        # narrow winner would understate the disagreement to every consumer
        # downstream, which is exactly the failure mode the resolver's
        # agreement_ratio exists to surface.
        return 0 if self.resolution.conflict else self.resolution.direction

    @property
    def conviction(self) -> float:
        """Weighted vote strength in [0, 1]. Not a position size."""
        return max(0.0, min(1.0, self.resolution.weight))

    @property
    def conflict(self) -> bool:
        return self.resolution.conflict

    @property
    def voting_ids(self) -> tuple[str, ...]:
        return tuple(v.strategy_id for v in self.verdicts if v.votes)

    def by_status(self, status: VerdictStatus) -> tuple[StrategyVerdict, ...]:
        return tuple(v for v in self.verdicts if v.status is status)

    def as_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for v in self.verdicts:
            counts[v.status.value] = counts.get(v.status.value, 0) + 1
        return {
            "direction": self.direction,
            "conviction": round(self.conviction, 6),
            "conflict": self.conflict,
            "agreement_ratio": round(self.resolution.agreement_ratio, 6),
            "raw_direction": self.resolution.direction,
            "voting": list(self.voting_ids),
            "status_counts": counts,
            "verdicts": [v.as_dict() for v in self.verdicts],
        }


@dataclass(frozen=True, slots=True)
class PortfolioInputs:
    """
    This tick's market state, in the union of forms the families need.

    Every field beyond ``symbol``/``timeframe`` is optional: a missing field
    abstains the families that depend on it and leaves the rest untouched.
    That is what lets the runner be wired in before every data source exists,
    instead of waiting for all of them and shipping none.
    """

    symbol: str
    timeframe: str
    highs: Sequence[float] | None = None
    lows: Sequence[float] | None = None
    closes: Sequence[float] | None = None
    volumes: Sequence[float] | None = None
    funding_rate_pct: float | None = None
    funding_history_pct: Sequence[float] | None = None
    spot_price: float | None = None
    perp_price: float | None = None
    # Observation times for the two basis legs, same rationale as
    # venue_price_ts: a spot/perp gap measured across a moving market is
    # latency, not basis.
    spot_price_ts: float | None = None
    perp_price_ts: float | None = None
    # Convergence horizon for the basis annualization; see
    # compute_annualized_basis_pct. Sets the scale of that family's signal.
    basis_days_to_convergence: float = 1.0
    # Trailing return per symbol across the traded universe. The target
    # symbol must appear in it for the cross-sectional family to rank itself.
    universe_returns: Mapping[str, float] = field(default_factory=dict)
    # Aligned close series for the mean-reversion pair, plus the hedge ratio
    # from the cointegration test that admitted it. All three or none: a
    # spread built from a stale hedge ratio is not the pair's spread.
    pair_closes_a: Sequence[float] | None = None
    pair_closes_b: Sequence[float] | None = None
    pair_hedge_ratio: float | None = None
    pair_window: int = 30
    # Same-symbol last price per venue, insertion-ordered. Two or more
    # entries are what let the cross-exchange family see a basis at all.
    venue_prices: Mapping[str, float] = field(default_factory=dict)
    # Wall-clock (epoch seconds, UTC) each venue price was observed. A basis
    # computed across two quotes taken seconds apart is a latency artifact,
    # not an arbitrage — see _MAX_VENUE_QUOTE_SKEW_S.
    venue_price_ts: Mapping[str, float] = field(default_factory=dict)
    extra: Mapping[str, Any] = field(default_factory=dict)


# A builder maps this tick's inputs onto one family's context type, or
# returns None to abstain it. Builders must not raise; the runner treats a
# raise as an error verdict rather than letting it reach the trade path.
ContextBuilder = Callable[[PortfolioInputs], object | None]


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _stdev(values: Sequence[float], mu: float) -> float:
    if len(values) < 2:
        return 0.0
    var = sum((v - mu) ** 2 for v in values) / (len(values) - 1)
    return var**0.5


def build_signal_engine_context(_inputs: PortfolioInputs) -> object | None:
    """The adapter is result-driven; it needs no context, only permission."""
    return NO_CONTEXT


def build_breakout_context(inputs: PortfolioInputs) -> object | None:
    series = (inputs.highs, inputs.lows, inputs.closes, inputs.volumes)
    if any(s is None for s in series):
        return None
    lengths = {len(s) for s in series if s is not None}
    # Ragged OHLCV means the bars were assembled inconsistently upstream.
    # Abstaining is right: silently truncating to the shortest would compare
    # a close against a high from a different bar.
    if len(lengths) != 1:
        return None
    (n,) = lengths
    if n < _MIN_BREAKOUT_BARS:
        return None
    return BreakoutContext(
        high=pd.Series(list(inputs.highs or []), dtype="float64"),
        low=pd.Series(list(inputs.lows or []), dtype="float64"),
        close=pd.Series(list(inputs.closes or []), dtype="float64"),
        volume=pd.Series(list(inputs.volumes or []), dtype="float64"),
    )


def build_funding_context(inputs: PortfolioInputs) -> object | None:
    rate = inputs.funding_rate_pct
    history = inputs.funding_history_pct
    if rate is None or history is None or len(history) < _MIN_FUNDING_SAMPLES:
        return None
    mu = _mean(history)
    sigma = _stdev(history, mu)
    # Constant funding history: a z-score is undefined, not zero. Zero would
    # read as "perfectly normal" and is the same value a genuinely
    # unremarkable rate produces, so abstain instead.
    #
    # Compared against the data's own scale, not against 0.0. Summing forty
    # copies of 0.01 does not land back on 0.01, so a genuinely constant
    # history yields sigma ~1e-18 rather than exactly zero — and dividing an
    # equally tiny (rate - mu) by it manufactured a confident z-score near -1
    # out of pure rounding noise.
    if sigma <= 1e-12 * max(abs(mu), 1.0):
        return None
    return FundingContext(funding_rate_pct=rate, funding_zscore=(rate - mu) / sigma)


def build_basis_trade_context(inputs: PortfolioInputs) -> object | None:
    """
    Pair the spot and perp legs, subject to the same staleness rule as the
    cross-exchange family — and for the same reason. Basis is the difference
    between two separately fetched prices, so a leg quoted seconds late turns
    a market move into an apparent carry.
    """
    if inputs.spot_price is None or inputs.perp_price is None:
        return None
    if inputs.spot_price <= 0.0 or inputs.perp_price <= 0.0:
        return None
    if (
        inputs.spot_price_ts is not None
        and inputs.perp_price_ts is not None
        and abs(inputs.spot_price_ts - inputs.perp_price_ts) > _MAX_VENUE_QUOTE_SKEW_S
    ):
        return None
    return BasisTradeContext(
        spot_price=inputs.spot_price,
        perp_price=inputs.perp_price,
        days_to_perp_funding_normalization=inputs.basis_days_to_convergence,
    )


def build_cross_exchange_context(inputs: PortfolioInputs) -> object | None:
    """
    Pair the two most recently quoted venues for this symbol.

    Two guards, both of which abstain rather than emit a number:

    * **Fewer than two venues.** One price is not a basis.
    * **Quote skew.** The two legs are separate network round-trips, so a
      price that moved between them shows up as a spread that was never
      tradeable. At a 15 bps entry threshold on a 100k asset that is a 150
      unit move — well inside what crypto does in a couple of seconds — so a
      stale pair manufactures exactly the signal this family looks for. The
      skew check is therefore load-bearing, not hygiene.
    """
    prices = {v: p for v, p in inputs.venue_prices.items() if p > 0.0}
    if len(prices) < 2:
        return None

    venues = list(prices)[:2]
    stamps = inputs.venue_price_ts
    if (
        all(v in stamps for v in venues)
        and abs(stamps[venues[0]] - stamps[venues[1]]) > _MAX_VENUE_QUOTE_SKEW_S
    ):
        return None

    return CrossExchangeContext(
        venue_a=venues[0],
        price_a=prices[venues[0]],
        venue_b=venues[1],
        price_b=prices[venues[1]],
    )


def build_mean_reversion_context(inputs: PortfolioInputs) -> object | None:
    """
    Assemble the pair's spread inputs.

    Abstains unless both legs and a hedge ratio are present, and unless the
    two series are the same length. Length is not a formality here: the
    spread is ``a - beta * b`` computed elementwise, so misaligned series
    would subtract one asset's price from another asset's price at a
    different time and hand the strategy a z-score of pure noise that looks
    exactly like a tradeable divergence.

    Whether the pair is *still* cointegrated is decided upstream, at data
    refresh, and reaches this function as the presence or absence of a hedge
    ratio — a decohered pair simply has none.
    """
    a, b = inputs.pair_closes_a, inputs.pair_closes_b
    if a is None or b is None or inputs.pair_hedge_ratio is None:
        return None
    if len(a) != len(b) or len(a) < inputs.pair_window + 1:
        return None
    return PairContext(
        price_a=pd.Series(list(a), dtype="float64"),
        price_b=pd.Series(list(b), dtype="float64"),
        hedge_ratio=inputs.pair_hedge_ratio,
        window=inputs.pair_window,
    )


def build_xsec_momentum_context(inputs: PortfolioInputs) -> object | None:
    """
    Rank this symbol against the universe's trailing returns.

    Abstains when the symbol being traded is absent from the cross-section.
    That is not a formality: the strategy ranks by percentile, so a missing
    target has no rank at all, and adding it at a neutral value would place
    it mid-universe — precisely where a decile strategy will never notice
    that its own asset was never measured.

    The universe size guard belongs to the strategy (_MIN_UNIVERSE_SIZE),
    not here, so a too-small universe reads as the strategy declining rather
    than as missing data.
    """
    returns = inputs.universe_returns
    if not returns or inputs.symbol not in returns:
        return None
    return UniverseContext(
        trailing_returns=pd.Series(dict(returns), dtype="float64"),
        target_symbol=inputs.symbol,
    )


class InputNeed(StrEnum):
    """A kind of data some family's context builder consumes."""

    BARS = "bars"
    FUNDING = "funding"
    VENUES = "venues"
    BASIS = "basis"
    UNIVERSE = "universe"
    PAIR = "pair"


# What each family actually reads. Declared next to the builders so the two
# cannot drift: a builder that starts consuming a new feed and forgets to
# declare it here will simply abstain, which is loud, rather than silently
# reading None.
BUILDER_NEEDS: dict[str, frozenset[InputNeed]] = {
    STRATEGY_ID_SIGNAL_ENGINE: frozenset(),
    "breakout_volume_v1": frozenset({InputNeed.BARS}),
    "funding_carry_v1": frozenset({InputNeed.FUNDING}),
    "basis_trade_v1": frozenset({InputNeed.BASIS}),
    "cross_exchange_arb_v1": frozenset({InputNeed.VENUES}),
    "xsec_momentum_v1": frozenset({InputNeed.UNIVERSE}),
    "mean_reversion_pairs_v1": frozenset({InputNeed.PAIR}),
}


def required_inputs(strategy_ids: Iterable[str]) -> frozenset[InputNeed]:
    """
    The union of feeds the given strategies need — nothing more.

    Assembling a tick's inputs is not free: it costs database reads and
    exchange round-trips, and it sits in front of the order path. On the
    default configuration only signal_engine_v1 is registered, and its
    builder consumes nothing at all, so fetching bars, funding history and
    two venue tickers every tick buys precisely nothing while adding network
    latency to every trade.

    An unknown strategy_id contributes no needs. That is the safe direction:
    it abstains the family for want of data rather than provoking fetches
    for a family this module has never heard of.
    """
    needs: set[InputNeed] = set()
    for sid in strategy_ids:
        needs |= BUILDER_NEEDS.get(sid, frozenset())
    return frozenset(needs)


def default_context_builders() -> dict[str, ContextBuilder]:
    """
    Builders for the families whose data this process already has.

    Families absent from this table (options carry) abstain with an explicit
    ``no_context_builder`` reason. They are not removed and not disabled:
    it needs a data source this process does not yet fetch — an options
    implied-vol surface — and the honest report is "cannot feed it yet", visible
    every tick, rather than deletion or a fabricated context.
    """
    return {
        STRATEGY_ID_SIGNAL_ENGINE: build_signal_engine_context,
        "breakout_volume_v1": build_breakout_context,
        "funding_carry_v1": build_funding_context,
        "basis_trade_v1": build_basis_trade_context,
        "cross_exchange_arb_v1": build_cross_exchange_context,
        "xsec_momentum_v1": build_xsec_momentum_context,
        "mean_reversion_pairs_v1": build_mean_reversion_context,
    }


class StrategyPortfolioRunner:
    """
    Polls every registered strategy and resolves their votes into one view.

    Never raises into the caller: a strategy that raises, a builder that
    raises, and a strategy with no data all become verdicts. The trade path
    must not be able to lose a valid incumbent signal because a
    non-incumbent family threw.
    """

    def __init__(
        self,
        *,
        registry: StrategyRegistry | None = None,
        resolver: HorizonConflictResolver | None = None,
        builders: Mapping[str, ContextBuilder] | None = None,
    ) -> None:
        self._registry = registry
        self._resolver = resolver or HorizonConflictResolver()
        self._builders: dict[str, ContextBuilder] = dict(
            default_context_builders() if builders is None else builders
        )

    @property
    def builders(self) -> Mapping[str, ContextBuilder]:
        return dict(self._builders)

    def register_context_builder(self, strategy_id: str, builder: ContextBuilder) -> None:
        """
        Teach the runner how to feed one more family.

        This is the extension point that retires an abstention: when a data
        source lands, register its builder and the family starts voting
        without any change to this class or to the orchestrator.
        """
        self._builders[strategy_id] = builder

    def _resolve_registry(self) -> StrategyRegistry:
        # Resolved per call, not at construction: the process-wide registry
        # is populated by bootstrap at startup, and a runner built before
        # that would otherwise hold an empty registry for the process's life.
        return self._registry if self._registry is not None else get_default_registry()

    def _poll(
        self,
        strategy: StrategyProtocol,
        inputs: PortfolioInputs,
        weight: float,
    ) -> StrategyVerdict:
        sid = strategy.strategy_id
        builder = self._builders.get(sid)
        if builder is None:
            return StrategyVerdict(sid, VerdictStatus.ABSTAINED, reason="no_context_builder")

        try:
            context = builder(inputs)
        except Exception as exc:
            log.warning("portfolio.context_build_failed", strategy_id=sid, error=str(exc))
            return StrategyVerdict(sid, VerdictStatus.ERROR, reason=f"context: {exc}")

        if context is None:
            return StrategyVerdict(sid, VerdictStatus.ABSTAINED, reason="insufficient_data")

        try:
            signal = strategy.generate_signal(context)
        except Exception as exc:
            log.warning("portfolio.generate_signal_failed", strategy_id=sid, error=str(exc))
            return StrategyVerdict(sid, VerdictStatus.ERROR, reason=f"signal: {exc}")

        if signal.regime_fit <= 0.0:
            # Documented as a hard gate on Signal.regime_fit, so it drops the
            # vote rather than scaling it — see module docstring.
            return StrategyVerdict(
                sid, VerdictStatus.REGIME_GATED, signal=signal, weight=weight, reason="regime_fit=0"
            )

        if signal.direction == 0 or signal.confidence <= 0.0:
            return StrategyVerdict(
                sid, VerdictStatus.FLAT, signal=signal, weight=weight, reason="no_direction"
            )

        return StrategyVerdict(sid, VerdictStatus.SIGNAL, signal=signal, weight=weight)

    def _resolve(self, verdicts: Sequence[StrategyVerdict]) -> ConflictResolution:
        """Fold a set of verdicts into one direction. Pure — polls nothing."""
        voting = [v for v in verdicts if v.votes and v.signal is not None]
        if not voting:
            # No voters is unanimity, not conflict: nobody disagreed.
            return ConflictResolution(direction=0, weight=0.0, conflict=False, agreement_ratio=1.0)
        payload = [
            {"direction": v.signal.direction, "confidence": v.signal.confidence}
            for v in voting
            if v.signal is not None
        ]
        # regime_fit multiplies the allocation weight: a family that is a
        # poor-but-permitted fit for the current regime should carry less of
        # the vote than the same family in its home regime.
        regime_weights = np.array(
            [max(v.weight, 1e-9) * (v.signal.regime_fit if v.signal else 0.0) for v in voting],
            dtype=float,
        )
        return self._resolver.resolve(payload, regime_weights=regime_weights)

    def resolve_excluding(
        self,
        evaluation: PortfolioEvaluation,
        exclude: set[str],
    ) -> PortfolioEvaluation:
        """
        Re-resolve an existing evaluation with some strategies muted.

        This is what the peer view needs, and re-polling was the wrong way to
        get it. Calling evaluate() a second time rebuilt every context —
        four pandas Series per breakout family, on the tick path — and, worse,
        asked every strategy the same question twice. Nothing enforces that a
        strategy is pure, so the two answers could differ, and then the
        agreement scalar would be sized against a vote that never happened.

        Polling once and resolving twice makes the peer view a strict subset
        of the full one by construction. Excluded strategies keep their
        verdicts — they were genuinely polled, and hiding that would misreport
        who was asked — they simply do not vote.
        """
        kept = tuple(
            v if v.strategy_id not in exclude else replace(v, status=VerdictStatus.DISABLED)
            for v in evaluation.verdicts
        )
        return PortfolioEvaluation(verdicts=kept, resolution=self._resolve(kept))

    def evaluate(
        self,
        inputs: PortfolioInputs,
        *,
        enabled_ids: set[str] | None = None,
        weights: Mapping[str, float] | None = None,
    ) -> PortfolioEvaluation:
        """
        Poll the portfolio once.

        enabled_ids : when given, ids outside it are reported DISABLED and
                      never polled — the kill switch must stop a strategy
                      from being consulted, not merely from being sized.
        weights     : allocation fractions per strategy_id. Missing ids fall
                      back to an equal share, so an allocator that has not
                      converged yet cannot mute a strategy outright.
        """
        strategies = tuple(self._resolve_registry().all())
        if not strategies:
            return PortfolioEvaluation(
                verdicts=(),
                resolution=ConflictResolution(
                    direction=0, weight=0.0, conflict=True, agreement_ratio=0.0
                ),
            )

        equal_share = 1.0 / len(strategies)
        verdicts: list[StrategyVerdict] = []
        for strategy in strategies:
            sid = strategy.strategy_id
            if enabled_ids is not None and sid not in enabled_ids:
                verdicts.append(
                    StrategyVerdict(sid, VerdictStatus.DISABLED, reason="not_in_enabled_set")
                )
                continue
            weight = equal_share if weights is None else float(weights.get(sid, equal_share))
            # A negative weight is not a short — it is a corrupt allocation.
            # Clamping keeps it from flipping another strategy's vote sign
            # inside the weighted average.
            verdicts.append(self._poll(strategy, inputs, max(0.0, weight)))

        evaluation = PortfolioEvaluation(
            verdicts=tuple(verdicts), resolution=self._resolve(verdicts)
        )
        log.info(
            "portfolio.evaluated",
            symbol=inputs.symbol,
            timeframe=inputs.timeframe,
            direction=evaluation.direction,
            conviction=round(evaluation.conviction, 4),
            conflict=evaluation.conflict,
            voting=list(evaluation.voting_ids),
        )
        return evaluation


_runner: StrategyPortfolioRunner | None = None
_runner_lock = threading.Lock()


def get_portfolio_runner() -> StrategyPortfolioRunner:
    """Process-wide runner, lazily constructed (mirrors get_default_registry)."""
    global _runner
    # Locked, not a bare check-then-assign: the two steps have a constructor
    # between them, so two callers arriving together could both find it unset
    # and both build a runner. A second runner allocating capital against the
    # same book is not a duplicate object, it is a second opinion nothing
    # reconciles.
    with _runner_lock:
        if _runner is None:
            _runner = StrategyPortfolioRunner()
        return _runner


def reset_portfolio_runner() -> None:
    """Drop the process-wide runner. Test-support only."""
    global _runner
    with _runner_lock:
        _runner = None


__all__ = [
    "BUILDER_NEEDS",
    "NO_CONTEXT",
    "ContextBuilder",
    "InputNeed",
    "PortfolioEvaluation",
    "PortfolioInputs",
    "StrategyPortfolioRunner",
    "StrategyVerdict",
    "VerdictStatus",
    "build_basis_trade_context",
    "build_breakout_context",
    "build_cross_exchange_context",
    "build_funding_context",
    "build_mean_reversion_context",
    "build_signal_engine_context",
    "build_xsec_momentum_context",
    "default_context_builders",
    "get_portfolio_runner",
    "required_inputs",
    "reset_portfolio_runner",
]
