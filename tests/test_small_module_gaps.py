"""Residual branches across small modules: provider cache/persist failures,
DuckDB transaction rollback, Granger's optional statsmodels import, the
CryptoBERT load failure, ECC head packing, conflict-resolver degenerate
weights, the Kyber placeholder, idempotency bookkeeping, Kyle-lambda guards,
causal fallbacks, the trade auditor's anomaly alerts and Ed25519 signing.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Provider cache publish / persist failures
# ---------------------------------------------------------------------------


def test_deribit_survives_a_provider_cache_that_refuses_the_chain(monkeypatch):
    from src.data.deribit_provider import DeribitProvider

    provider = DeribitProvider()
    chain = pd.DataFrame([{"strike": 50_000.0, "iv": 0.6}])

    async def _chain(_coin):
        return chain

    provider._fetch_chain = _chain
    monkeypatch.setattr(
        "src.data.provider_cache.get_provider_cache",
        lambda: (_ for _ in ()).throw(RuntimeError("cache down")),
    )

    assert asyncio.run(provider.fetch("BTC/USDT")) is chain


def test_deribit_orderbook_returns_none_when_the_request_fails():
    from src.data.deribit_provider import DeribitProvider

    class _Session:
        def get(self, *_a, **_kw):
            raise RuntimeError("connection reset")

    result = asyncio.run(DeribitProvider()._get_orderbook(_Session(), "BTC-30AUG24-50000-C"))
    assert result is None


def test_exchange_flow_cache_publish_failure_is_swallowed(monkeypatch):
    from src.data.exchange_flow_provider import ExchangeFlowProvider

    provider = ExchangeFlowProvider()
    monkeypatch.setattr(
        "src.data.provider_cache.get_provider_cache",
        lambda: (_ for _ in ()).throw(RuntimeError("cache down")),
    )

    provider._update_cache()  # must not raise


def test_exchange_flow_persist_failure_is_swallowed(tmp_path, monkeypatch):
    from src.data import exchange_flow_provider as mod

    provider = mod.ExchangeFlowProvider(data_root=tmp_path)
    provider._flows = [{"exchange": "binance", "netflow_usd": 1.0}]
    monkeypatch.setattr(
        mod.pd,
        "DataFrame",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("no parquet engine")),
    )

    provider._persist()  # best-effort; the loop must keep running


# ---------------------------------------------------------------------------
# DuckDB store
# ---------------------------------------------------------------------------


def test_a_failed_transaction_is_rolled_back(tmp_path):
    from src.data.duckdb_store import DuckDBStore

    store = DuckDBStore(path=tmp_path / "t.duckdb")

    def _failing_tx():
        with store._tx():
            raise RuntimeError("boom")

    try:
        with pytest.raises(RuntimeError, match="boom"):
            _failing_tx()
        # the connection is still usable after the rollback
        store.write_feature_log(symbol="BTC/USDT", features={"ofi": 1.0})
    finally:
        store.close()


def test_writing_an_empty_feature_dict_is_a_no_op(tmp_path):
    from src.data.duckdb_store import DuckDBStore

    store = DuckDBStore(path=tmp_path / "t.duckdb")
    try:
        store.write_feature_log(symbol="BTC/USDT", features={})
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Granger
# ---------------------------------------------------------------------------


def test_granger_is_disabled_when_statsmodels_is_missing(monkeypatch):
    from src.causal.granger import GrangerCausalityDetector

    detector = GrangerCausalityDetector()
    monkeypatch.setitem(sys.modules, "statsmodels.tsa.stattools", None)

    btc = pd.Series(np.random.default_rng(0).normal(0, 0.01, 200))
    assert detector.update(btc, {"ETH/USDT": list(range(200))}) == {}


def test_granger_skips_an_alt_series_that_is_too_short():
    from src.causal.granger import GrangerCausalityDetector

    detector = GrangerCausalityDetector()
    btc = pd.Series(np.random.default_rng(1).normal(0, 0.01, 200))

    out = detector.update(btc, {"ETH/USDT": [100.0, 101.0, 102.0]})
    assert "ETH/USDT" not in out


# ---------------------------------------------------------------------------
# NLP / ECC head
# ---------------------------------------------------------------------------


def test_cryptobert_reports_unavailable_when_the_model_cannot_load(monkeypatch):
    from src.features import nlp

    # a None entry makes `from transformers import ...` raise, which is the
    # same shape as a machine without the optional model dependency
    monkeypatch.setitem(sys.modules, "transformers", None)
    pipeline = nlp._CryptoBERTPipeline()

    assert pipeline._available is False


def test_ecc_features_pack_into_a_single_row_tensor():
    import torch

    from src.models.ecc_head import build_ecc_feature_tensor

    out = build_ecc_feature_tensor(
        cluster_flow_score=0.1,
        ecdsa_weakness=0.2,
        schnorr_divergence=0.3,
        hodler_index=0.4,
        dark_pool_pressure=0.5,
    )

    assert out.shape == (1, 5)
    assert out.dtype is torch.float32
    assert out[0, 4].item() == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Conflict resolver
# ---------------------------------------------------------------------------


def _signals(n: int = 3, direction: int = 1, confidence: float = 0.8) -> list[dict]:
    return [{"direction": direction, "confidence": confidence, "horizon_idx": i} for i in range(n)]


def test_mismatched_regime_weights_fall_back_to_uniform():
    from src.risk.conflict_resolver import HorizonConflictResolver

    resolver = HorizonConflictResolver()
    out = resolver.resolve(_signals(3), np.array([0.5]))  # one weight for three signals

    assert out.direction == 1
    assert out.agreement_ratio == pytest.approx(1.0)


def test_all_zero_weights_resolve_to_flat_and_conflicted():
    from src.risk.conflict_resolver import HorizonConflictResolver

    resolver = HorizonConflictResolver()
    out = resolver.resolve(_signals(3, confidence=0.0), np.zeros(3))

    assert out.direction == 0
    assert out.conflict is True
    assert out.weight == 0.0


# ---------------------------------------------------------------------------
# Kyber placeholder
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "args"),
    [("keygen", ()), ("encapsulate", (b"pk",)), ("decapsulate", (b"sk", b"ct"))],
)
def test_kyber_transport_refuses_until_liboqs_is_wired(method, args):
    from src.security.pq_transport import PQTransportStub

    with pytest.raises(RuntimeError, match="Kyber-768"):
        getattr(PQTransportStub(), method)(*args)


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_completing_an_unreserved_key_still_records_the_order():
    from src.execution.idempotency import IdempotencyRegistry, SubmissionState

    store = IdempotencyRegistry()
    asyncio.run(store.complete("k1", order_id="o1", result={"status": "filled"}))

    record = store._records["k1"]
    assert record.state is SubmissionState.COMPLETED
    assert record.order_id == "o1"


def test_failing_an_unreserved_key_keeps_it_claimed_when_not_retryable():
    from src.execution.idempotency import IdempotencyRegistry, SubmissionState

    store = IdempotencyRegistry()
    asyncio.run(store.fail("k2", "timeout", retryable=False))

    assert store._records["k2"].state is SubmissionState.COMPLETED


# ---------------------------------------------------------------------------
# Kyle lambda guards
# ---------------------------------------------------------------------------


def test_kyle_lambda_holds_its_estimate_when_signed_volume_has_no_variance():
    from src.features.microstructure import KyleLambdaEstimator

    est = KyleLambdaEstimator()
    for i in range(40):
        est.update(50_000.0 + i, 1.0)  # constant volume -> zero variance

    assert est.lambda_ == 0.0


# ---------------------------------------------------------------------------
# Causal inference fallbacks
# ---------------------------------------------------------------------------


def test_backdoor_adjustment_falls_back_to_correlation_without_sklearn(monkeypatch):
    from src.intelligence.causal_inference import CausalInferenceEngine

    rng = np.random.default_rng(2)
    treatment = rng.normal(size=60)
    outcome = treatment * 0.5 + rng.normal(0, 0.1, 60)
    confounders = rng.normal(size=(60, 2))

    monkeypatch.setitem(sys.modules, "sklearn.linear_model", None)
    effect = CausalInferenceEngine()._backdoor_adjustment(treatment, outcome, confounders)

    assert effect == pytest.approx(float(np.corrcoef(treatment, outcome)[0, 1]))


# ---------------------------------------------------------------------------
# Trade auditor anomaly alerts
# ---------------------------------------------------------------------------


def _audit_record(**overrides):
    from src.diagnostics.trade_auditor import AuditRecord

    base = {
        "ts_utc": datetime.now(tz=UTC).timestamp(),
        "symbol": "BTC/USDT",
        "timeframe": "15m",
        "features": {},
        "p_long": 0.5,
        "p_bet": 0.5,
        "direction": 1,
        "regime_state": 1,
        "prob_ranging": 0.1,
        "prob_trending": 0.8,
        "prob_volatile": 0.1,
        "gate_status": "PASS",
        "gate_reason": "",
        "gate_details": {},
        "kelly_fraction": 0.05,
        "kelly_notional_usd": 500.0,
        "kelly_quantity": 0.1,
        "kelly_is_capped": False,
        "outcome": "opened",
        "trade_id": None,
        "skip_reason": "",
    }
    return AuditRecord(**(base | overrides))


def test_a_degenerate_direction_model_raises_both_anomaly_alerts():
    from src.diagnostics.trade_auditor import TradeAuditor

    auditor = TradeAuditor()
    for _ in range(60):
        auditor.record(_audit_record(p_long=0.5, p_bet=0.5))

    alerts = auditor.anomaly_scan()

    assert any(a.startswith("p_long_variance_collapsed") for a in alerts)
    assert any(a.startswith("meta_label_not_discriminating") for a in alerts)


# ---------------------------------------------------------------------------
# Ed25519 API signer
# ---------------------------------------------------------------------------


def test_a_supplied_seed_round_trips_a_signature():
    import base64

    from src.security.api_signer import ApiSigner

    seed = base64.b64encode(bytes(range(32))).decode()
    signer = ApiSigner(seed)
    sig = signer.sign_request("GET", "/health", "", 1_700_000_000)

    assert ApiSigner(seed).verify("GET", "/health", "", 1_700_000_000, sig) is True
    # deterministic: the same key and message always produce the same signature
    assert ApiSigner(seed).sign_request("GET", "/health", "", 1_700_000_000) == sig


def test_from_env_uses_the_configured_key(monkeypatch):
    import base64

    from src.security.api_signer import ApiSigner

    seed = base64.b64encode(bytes(range(1, 33))).decode()
    monkeypatch.setenv("API_SIGNING_KEY_B64", seed)

    assert ApiSigner.from_env().public_key_b64() == ApiSigner(seed).public_key_b64()


def test_from_env_falls_back_to_an_ephemeral_key(monkeypatch):
    from src.security.api_signer import ApiSigner

    monkeypatch.delenv("API_SIGNING_KEY_B64", raising=False)

    signer = ApiSigner.from_env()
    sig = signer.sign_request("POST", "/orders", "{}", 1)
    assert signer.verify("POST", "/orders", "{}", 1, sig) is True


def test_no_signals_resolve_to_flat():
    from src.risk.conflict_resolver import HorizonConflictResolver

    out = HorizonConflictResolver().resolve([], None)
    assert out.direction == 0
    assert out.conflict is True


def test_confidence_is_used_as_the_weight_when_no_regime_weights_are_given():
    from src.risk.conflict_resolver import HorizonConflictResolver

    out = HorizonConflictResolver().resolve(_signals(3), None)
    assert out.direction == 1


def test_a_dead_heat_reports_a_neutral_agreement():
    from src.risk.conflict_resolver import HorizonConflictResolver

    signals = [
        {"direction": 1, "confidence": 0.8, "horizon_idx": 0},
        {"direction": -1, "confidence": 0.8, "horizon_idx": 1},
    ]
    out = HorizonConflictResolver().resolve(signals, None)

    assert out.direction == 0
    assert out.agreement_ratio == pytest.approx(0.5)


def test_an_always_capped_kelly_is_reported_as_an_anomaly():
    from src.diagnostics.trade_auditor import TradeAuditor

    auditor = TradeAuditor()
    rng = np.random.default_rng(11)
    for _ in range(60):
        auditor.record(
            _audit_record(
                p_long=float(rng.uniform(0.2, 0.8)),
                p_bet=float(rng.uniform(0.2, 0.8)),
                kelly_is_capped=True,
            )
        )

    assert any(a.startswith("kelly_ceiling_always_binding") for a in auditor.anomaly_scan())


def test_get_auditor_returns_one_process_wide_instance():
    from src.diagnostics import trade_auditor

    trade_auditor.get_auditor.cache_clear()
    first = trade_auditor.get_auditor()

    assert trade_auditor.get_auditor() is first
