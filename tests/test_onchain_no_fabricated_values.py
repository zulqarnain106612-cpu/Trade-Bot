"""REG-0008: on-chain features never report a fabricated constant.

`_approximate_sopr` returned a literal 1.0 on every call, ignoring its only
argument, and that value flowed into the live feature dict in src/intel.py and
was persisted to `intelligence_features_history` beside genuinely measured
columns. The RPC-failure path did the same thing with 1.0/50.0/1.0, so a node
outage was indistinguishable from a neutral on-chain reading.

This is the fabricated-completion-state bug GAP-015 fixed in
src/intelligence/metrics.py and the Binance `whale_buy_sell_ratio` fix fixed in
src/intelligence/providers/binance_provider.py. Those two are guarded; this
third instance was not, and the test that existed asserted the placeholder
(`assert extractor._approximate_sopr(50_000.0) == 1.0`) rather than the
requirement.

The value-level cases are pinned below, and so is the structural property, so
the next constant cannot be reintroduced under a different name.
"""

from __future__ import annotations

import ast
import math
import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.features.onchain import OnChainFeatureExtractor

_SOURCE = pathlib.Path("src/features/onchain.py")


@pytest.fixture(scope="module")
def onchain_tree() -> ast.Module:
    """Parsed once per module -- every structural case reads this same tree."""
    return ast.parse(_SOURCE.read_text(encoding="utf-8"))


def _fake_rpc(utxos: list[dict] | None = None) -> MagicMock:
    rpc = MagicMock()
    rpc.get_blockchain_info = AsyncMock(return_value={"bestblockhash": "abc"})
    rpc.get_block_stats = AsyncMock(return_value={"total_out": 5_000_000_000})
    rpc.list_unspent = AsyncMock(return_value=utxos if utxos is not None else [])
    return rpc


def test_sopr_is_nan_not_a_constant() -> None:
    assert math.isnan(OnChainFeatureExtractor(rpc=_fake_rpc())._sopr_unimplemented())


async def test_sopr_is_nan_on_the_happy_path_too() -> None:
    """A reachable node does not make SOPR computable -- it is NaN either way."""
    feats = await OnChainFeatureExtractor(rpc=_fake_rpc([{"amount": 3.0}])).compute(
        spot_price_usd=50_000.0, market_cap_usd=1e12
    )
    assert math.isnan(feats.sopr)
    assert not math.isnan(feats.nvt), "NVT is genuinely computed and must stay so"
    assert not math.isnan(feats.mvrv), "MVRV is computable when UTXOs exist"


async def test_node_outage_is_nan_not_a_neutral_looking_reading() -> None:
    rpc = _fake_rpc()
    rpc.get_blockchain_info = AsyncMock(side_effect=RuntimeError("node down"))
    feats = await OnChainFeatureExtractor(rpc=rpc).compute(50_000.0, 1e12)
    assert all(math.isnan(v) for v in (feats.sopr, feats.nvt, feats.mvrv))


def test_realised_cap_with_no_utxos_is_nan() -> None:
    """Nothing to sum is not the same as half the 21M supply priced at spot."""
    assert math.isnan(OnChainFeatureExtractor(rpc=_fake_rpc())._estimate_realised_cap([], 50_000.0))


async def test_missing_realised_cap_propagates_to_mvrv() -> None:
    """The NaN must survive `max(realised_cap, 1.0)` rather than becoming 1.0."""
    feats = await OnChainFeatureExtractor(rpc=_fake_rpc([])).compute(50_000.0, 1e12)
    assert math.isnan(feats.mvrv)


@pytest.mark.parametrize("metric", ["sopr", "nvt", "mvrv"])
async def test_every_metric_varies_or_is_nan(metric: str) -> None:
    """A metric that answers identically to different inputs is not a metric.

    Either it responds to the market (NVT, MVRV) or it declares itself absent
    (SOPR). What it may never be is a finite number that ignores its inputs.
    """
    low = await OnChainFeatureExtractor(rpc=_fake_rpc([{"amount": 1.0}])).compute(10_000.0, 1e11)
    high = await OnChainFeatureExtractor(rpc=_fake_rpc([{"amount": 1.0}])).compute(90_000.0, 9e12)
    a, b = getattr(low, metric), getattr(high, metric)
    assert math.isnan(a) or a != b, f"{metric} is constant across different markets"


def test_no_function_returns_a_bare_numeric_literal(onchain_tree: ast.Module) -> None:
    """The structural half: a number that varies with nothing cannot come back.

    Catches the reintroduction under any name, which the value-level cases
    above cannot -- they only know the names that exist today.
    """
    offenders = []
    for node in ast.walk(onchain_tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        returns = [n for n in ast.walk(node) if isinstance(n, ast.Return) and n.value is not None]
        if not returns:
            continue
        if all(
            isinstance(r.value, ast.Constant)
            and isinstance(r.value.value, int | float)
            and not isinstance(r.value.value, bool)
            for r in returns
        ):
            offenders.append(f"{node.name}() -> {[r.value.value for r in returns]}")
    assert not offenders, f"fabricated constant(s) in {_SOURCE}: {offenders}"


def test_docstrings_do_not_promise_a_computation_that_is_absent(
    onchain_tree: ast.Module,
) -> None:
    """The original docstring claimed a momentum perturbation the body never did.

    A docstring is how the next reader decides whether a number is real, so a
    false one is the same defect as the false number.
    """
    banned = ("perturbation", "± small", "rough mid-point")
    for node in ast.walk(onchain_tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Module):
            doc = ast.get_docstring(node) or ""
            hits = [w for w in banned if w in doc]
            assert not hits, f"{getattr(node, 'name', '<module>')} claims {hits}"
