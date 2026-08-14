"""Coverage for the Tornado Cash mixing detector.

This module produces the dark_pool_pressure feature the RL state consumes, so
its classification and clustering logic is worth pinning down.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.ecc.zksnark_detect import (
    _TORNADO_CONTRACTS,
    MixingFlowResult,
    ZkSnarkDetector,
    _is_tornado_deposit,
    _is_tornado_withdrawal,
    estimate_dark_pool_pressure,
    trace_spend_cluster,
)


_POOL = next(iter(sorted(_TORNADO_CONTRACTS)))
_DEPOSIT_SELECTOR = "0xb214faa5"
_WITHDRAW_SELECTOR = "0x21a0adb5"


def _tx(
    to: str = _POOL, data: str = _DEPOSIT_SELECTOR, value: int = 10**18, h: str = "0x1"
) -> dict:
    return {"to": to, "input": data + "00" * 4, "value": value, "hash": h}


class TestClassification:
    def test_a_pool_deposit_is_recognised(self) -> None:
        assert _is_tornado_deposit(_tx())

    def test_the_pool_address_match_is_case_insensitive(self) -> None:
        assert _is_tornado_deposit(_tx(to=_POOL.upper()))

    def test_a_non_pool_address_is_not_a_deposit(self) -> None:
        assert not _is_tornado_deposit(_tx(to="0xdeadbeef"))

    def test_a_missing_recipient_is_not_a_deposit(self) -> None:
        assert not _is_tornado_deposit({"to": None, "input": _DEPOSIT_SELECTOR})

    def test_a_pool_call_with_another_selector_is_not_a_deposit(self) -> None:
        assert not _is_tornado_deposit(_tx(data="0xdeadbeef"))

    def test_a_withdrawal_is_recognised(self) -> None:
        assert _is_tornado_withdrawal(_tx(data=_WITHDRAW_SELECTOR))

    def test_a_deposit_is_not_a_withdrawal(self) -> None:
        assert not _is_tornado_withdrawal(_tx())

    def test_a_non_pool_withdrawal_selector_is_ignored(self) -> None:
        assert not _is_tornado_withdrawal(_tx(to="0xdeadbeef", data=_WITHDRAW_SELECTOR))


class TestDarkPoolPressure:
    def test_an_empty_cluster_has_no_pressure(self) -> None:
        assert estimate_dark_pool_pressure([]) == 0.0

    def test_pressure_scales_with_cluster_size(self) -> None:
        assert estimate_dark_pool_pressure([{}] * 25) == 0.5

    def test_pressure_saturates_at_one(self) -> None:
        assert estimate_dark_pool_pressure([{}] * 500) == 1.0


class TestTraceSpendCluster:
    def test_a_matching_denomination_withdrawal_joins_the_cluster(self) -> None:
        deposit = _tx(h="0xdep")
        withdrawal = _tx(data=_WITHDRAW_SELECTOR, h="0xw1")
        assert trace_spend_cluster(deposit, [withdrawal]) == [withdrawal]

    def test_a_different_denomination_is_excluded(self) -> None:
        deposit = _tx(value=10**18, h="0xdep")
        withdrawal = _tx(data=_WITHDRAW_SELECTOR, value=99 * 10**18, h="0xw1")
        assert trace_spend_cluster(deposit, [withdrawal]) == []

    def test_non_withdrawals_are_excluded(self) -> None:
        assert trace_spend_cluster(_tx(h="0xdep"), [_tx(h="0xother")]) == []

    def test_the_deposit_itself_is_never_clustered(self) -> None:
        deposit = _tx(data=_WITHDRAW_SELECTOR, h="0xdep")
        assert trace_spend_cluster(deposit, [deposit]) == []

    def test_hex_string_values_are_parsed(self) -> None:
        deposit = {"hash": "0xdep", "value": hex(10**18), "to": _POOL, "input": _DEPOSIT_SELECTOR}
        withdrawal = {
            "hash": "0xw1",
            "value": hex(10**18),
            "to": _POOL,
            "input": _WITHDRAW_SELECTOR,
        }
        assert trace_spend_cluster(deposit, [withdrawal]) == [withdrawal]

    def test_zero_depth_traces_nothing(self) -> None:
        withdrawal = _tx(data=_WITHDRAW_SELECTOR, h="0xw1")
        assert trace_spend_cluster(_tx(h="0xdep"), [withdrawal], depth=0) == []

    def test_each_withdrawal_is_counted_once(self) -> None:
        deposit = _tx(h="0xdep")
        withdrawals = [_tx(data=_WITHDRAW_SELECTOR, h=f"0xw{i}") for i in range(3)]
        assert len(trace_spend_cluster(deposit, withdrawals, depth=3)) == 3


class TestDetector:
    def _detector(self, w3: MagicMock | None) -> ZkSnarkDetector:
        with patch.object(ZkSnarkDetector, "_load_web3", lambda self: None):
            detector = ZkSnarkDetector()
        detector._w3 = w3
        detector._available = w3 is not None
        return detector

    def test_an_unavailable_node_yields_a_neutral_result(self) -> None:
        result = self._detector(None).detect_mixing_flows()
        assert result == MixingFlowResult(0.0, 0, 0.0, 0)

    def test_deposits_in_recent_blocks_are_counted(self) -> None:
        w3 = MagicMock()
        w3.eth.block_number = 2
        w3.eth.get_block.return_value = MagicMock(transactions=[_tx(h="0xdep")])
        result = self._detector(w3).detect_mixing_flows(block_lookback=1)
        assert result.tornado_deposits_detected == 2  # one per scanned block
        assert result.estimated_mixed_eth == 2.0
        assert result.dark_pool_pressure > 0.0

    def test_blocks_without_pool_activity_stay_neutral(self) -> None:
        w3 = MagicMock()
        w3.eth.block_number = 1
        w3.eth.get_block.return_value = MagicMock(transactions=[_tx(to="0xdeadbeef")])
        result = self._detector(w3).detect_mixing_flows(block_lookback=0)
        assert result.tornado_deposits_detected == 0
        assert result.dark_pool_pressure == 0.0

    def test_an_rpc_fault_yields_a_neutral_result(self) -> None:
        w3 = MagicMock()
        type(w3.eth).block_number = property(
            lambda _: (_ for _ in ()).throw(RuntimeError("node down"))
        )
        assert self._detector(w3).detect_mixing_flows() == MixingFlowResult(0.0, 0, 0.0, 0)


class TestWeb3Loading:
    def test_a_missing_web3_package_leaves_the_detector_unavailable(self) -> None:
        with patch.dict("sys.modules", {"web3": None}):
            detector = ZkSnarkDetector()
        assert detector._available is False

    def test_a_disconnected_node_leaves_the_detector_unavailable(self) -> None:
        web3_mod = MagicMock()
        web3_mod.Web3.return_value.is_connected.return_value = False
        with patch.dict("sys.modules", {"web3": web3_mod}):
            detector = ZkSnarkDetector()
        assert detector._available is False

    def test_a_connected_node_marks_the_detector_available(self) -> None:
        web3_mod = MagicMock()
        web3_mod.Web3.return_value.is_connected.return_value = True
        with patch.dict("sys.modules", {"web3": web3_mod}):
            detector = ZkSnarkDetector()
        assert detector._available is True
