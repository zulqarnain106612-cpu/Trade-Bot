"""Tests for src/diagnostics/system_health.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.diagnostics.system_health import (
    STATUS_DEGRADED,
    STATUS_OK,
    STATUS_UNKNOWN,
    HealthComponent,
    SystemHealthReport,
    build_system_health,
    check_kill_switch,
    check_macro_budget,
    check_order_throttler,
)


# ---------------------------------------------------------------------------
# HealthComponent
# ---------------------------------------------------------------------------


def test_health_component_to_dict() -> None:
    c = HealthComponent(name="foo", status=STATUS_OK, message="", details={"k": 1})
    d = c.to_dict()
    assert d["name"] == "foo"
    assert d["status"] == STATUS_OK
    assert d["details"] == {"k": 1}


# ---------------------------------------------------------------------------
# SystemHealthReport
# ---------------------------------------------------------------------------


def test_report_overall_ok() -> None:
    r = SystemHealthReport(
        components=[
            HealthComponent(name="a", status=STATUS_OK),
            HealthComponent(name="b", status=STATUS_OK),
        ]
    )
    assert r.overall_status == STATUS_OK
    assert r.degraded_components == []


def test_report_overall_degraded() -> None:
    r = SystemHealthReport(
        components=[
            HealthComponent(name="a", status=STATUS_OK),
            HealthComponent(name="b", status=STATUS_DEGRADED),
        ]
    )
    assert r.overall_status == STATUS_DEGRADED
    assert r.degraded_components == ["b"]


def test_report_empty_is_unknown() -> None:
    assert SystemHealthReport().overall_status == STATUS_UNKNOWN


def test_report_to_dict_keys() -> None:
    r = SystemHealthReport(components=[HealthComponent(name="x", status=STATUS_OK)])
    d = r.to_dict()
    assert "overall_status" in d
    assert "degraded_components" in d
    assert "components" in d


# ---------------------------------------------------------------------------
# check_kill_switch
# ---------------------------------------------------------------------------


def _make_ks(statuses: dict) -> MagicMock:
    ks = MagicMock()
    ks.all_statuses.return_value = statuses
    ks.is_active.return_value = True
    return ks


def test_kill_switch_all_ok() -> None:
    ks = _make_ks({"s1/1h": {"is_paused": False}})
    with patch("src.risk.strategy_kill_switch.get_kill_switch", return_value=ks):
        c = check_kill_switch()
    assert c.status == STATUS_OK
    assert c.details["n_paused"] == 0


def test_kill_switch_paused() -> None:
    ks = _make_ks({"s1/1h": {"is_paused": True}})
    with patch("src.risk.strategy_kill_switch.get_kill_switch", return_value=ks):
        c = check_kill_switch()
    assert c.status == STATUS_DEGRADED
    assert c.details["n_paused"] == 1


def test_kill_switch_per_strategy_active() -> None:
    ks = MagicMock()
    ks.is_active.return_value = True
    with patch("src.risk.strategy_kill_switch.get_kill_switch", return_value=ks):
        c = check_kill_switch("BTC/USDT", "1h")
    assert c.status == STATUS_OK
    assert c.details["active"] is True


def test_kill_switch_per_strategy_paused() -> None:
    ks = MagicMock()
    ks.is_active.return_value = False
    with patch("src.risk.strategy_kill_switch.get_kill_switch", return_value=ks):
        c = check_kill_switch("BTC/USDT", "1h")
    assert c.status == STATUS_DEGRADED


def test_kill_switch_exception_returns_unknown() -> None:
    with patch("src.risk.strategy_kill_switch.get_kill_switch", side_effect=RuntimeError("boom")):
        c = check_kill_switch()
    assert c.status == STATUS_UNKNOWN
    assert "boom" in c.message


# ---------------------------------------------------------------------------
# check_macro_budget
# ---------------------------------------------------------------------------


def _make_registry(util_pct: float) -> MagicMock:
    reg = MagicMock()
    reg.summary.return_value = {"global_utilisation_pct": util_pct}
    return reg


def test_macro_budget_ok() -> None:
    reg = _make_registry(50.0)
    with patch("src.risk.macro_exposure_budget._REGISTRY", reg):
        c = check_macro_budget(warn_utilisation_pct=80.0)
    assert c.status == STATUS_OK


def test_macro_budget_degraded() -> None:
    reg = _make_registry(90.0)
    with patch("src.risk.macro_exposure_budget._REGISTRY", reg):
        c = check_macro_budget(warn_utilisation_pct=80.0)
    assert c.status == STATUS_DEGRADED
    assert "90.0%" in c.message


def test_macro_budget_none_registry() -> None:
    with patch("src.risk.macro_exposure_budget._REGISTRY", None):
        c = check_macro_budget()
    assert c.status == STATUS_UNKNOWN


def test_macro_budget_exception() -> None:
    with patch(
        "src.risk.macro_exposure_budget._REGISTRY",
        MagicMock(summary=MagicMock(side_effect=ValueError("err"))),
    ):
        c = check_macro_budget()
    assert c.status == STATUS_UNKNOWN


# ---------------------------------------------------------------------------
# check_order_throttler
# ---------------------------------------------------------------------------


def test_order_throttler_ok() -> None:
    t = MagicMock()
    t.tokens_remaining.return_value = 5.0
    with patch("src.execution.order_throttler.OrderThrottler", return_value=t):
        c = check_order_throttler()
    assert c.status == STATUS_OK
    assert c.details["tokens_remaining"] == 5.0


def test_order_throttler_degraded() -> None:
    t = MagicMock()
    t.tokens_remaining.return_value = 0.5
    with patch("src.execution.order_throttler.OrderThrottler", return_value=t):
        c = check_order_throttler()
    assert c.status == STATUS_DEGRADED


def test_order_throttler_exception() -> None:
    with patch("src.execution.order_throttler.OrderThrottler", side_effect=ImportError("no mod")):
        c = check_order_throttler()
    assert c.status == STATUS_UNKNOWN


# ---------------------------------------------------------------------------
# build_system_health integration
# ---------------------------------------------------------------------------


def test_build_system_health_returns_report() -> None:
    ks = _make_ks({})
    reg = _make_registry(10.0)
    t = MagicMock()
    t.tokens_remaining.return_value = 5.0

    with (
        patch("src.risk.strategy_kill_switch.get_kill_switch", return_value=ks),
        patch("src.risk.macro_exposure_budget._REGISTRY", reg),
        patch("src.execution.order_throttler.OrderThrottler", return_value=t),
    ):
        report = build_system_health()

    assert report.overall_status == STATUS_OK
    names = {c.name for c in report.components}
    assert names == {"kill_switch", "macro_budget", "order_throttler"}


def test_build_system_health_propagates_degraded() -> None:
    ks = _make_ks({"s/1h": {"is_paused": True}})
    reg = _make_registry(10.0)
    t = MagicMock()
    t.tokens_remaining.return_value = 5.0

    with (
        patch("src.risk.strategy_kill_switch.get_kill_switch", return_value=ks),
        patch("src.risk.macro_exposure_budget._REGISTRY", reg),
        patch("src.execution.order_throttler.OrderThrottler", return_value=t),
    ):
        report = build_system_health()

    assert report.overall_status == STATUS_DEGRADED
    assert "kill_switch" in report.degraded_components
