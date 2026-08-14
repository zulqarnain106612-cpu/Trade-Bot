"""
Tests for the audit-chain integrity endpoint.

audit_trail.py hash-chains every entry so tampering is detectable, and
SignalEngine writes to it every tick — but `verify_chain_integrity()` had no
caller and the trail had no reader. The hashing cost was paid on every tick
and the guarantee it buys was never collected.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

import pytest

from src.diagnostics.audit_trail import AuditTrail


def _trail(n: int = 3) -> AuditTrail:
    trail = AuditTrail()
    for i in range(n):
        trail.record(
            event_type="risk_gate_fired",
            reason_code=f"gate_{i}",
            details={"i": i},
        )
    return trail


async def _call(trail: AuditTrail, limit: int = 50):
    from src.api.main import audit_chain_integrity

    with patch("src.api.main.get_audit_trail", return_value=trail):
        return await audit_chain_integrity(limit=limit)


class TestIntactChain:
    @pytest.mark.asyncio
    async def test_an_empty_trail_is_intact(self) -> None:
        result = await _call(AuditTrail())
        assert result["intact"] is True
        assert result["first_broken_sequence"] is None
        assert result["entry_count"] == 0

    @pytest.mark.asyncio
    async def test_an_untampered_trail_verifies(self) -> None:
        result = await _call(_trail(5))
        assert result["intact"] is True
        assert result["first_broken_sequence"] is None
        assert result["entry_count"] == 5

    @pytest.mark.asyncio
    async def test_the_tail_is_returned_newest_last(self) -> None:
        result = await _call(_trail(5), limit=2)
        assert [e["reason_code"] for e in result["recent"]] == ["gate_3", "gate_4"]

    @pytest.mark.asyncio
    async def test_entry_hashes_are_exposed_for_external_checking(self) -> None:
        """An operator must be able to re-verify without trusting this endpoint."""
        result = await _call(_trail(2))
        assert all(len(e["entry_hash"]) == 64 for e in result["recent"])


class TestTamperDetection:
    def _tampered(self) -> AuditTrail:
        """Rewrite a historical entry's details, leaving its hash stale."""
        trail = _trail(4)
        trail._entries[1] = replace(trail._entries[1], details={"i": "altered"})
        return trail

    @pytest.mark.asyncio
    async def test_a_rewritten_entry_is_detected(self) -> None:
        result = await _call(self._tampered())
        assert result["intact"] is False

    @pytest.mark.asyncio
    async def test_the_first_broken_sequence_is_reported(self) -> None:
        """Which entry broke matters more than that something did."""
        trail = self._tampered()
        broken_seq = trail._entries[1].sequence
        result = await _call(trail)
        assert result["first_broken_sequence"] == broken_seq

    @pytest.mark.asyncio
    async def test_a_severed_link_is_detected_even_with_a_valid_own_hash(self) -> None:
        """
        Deleting a middle entry leaves every remaining hash internally valid;
        only the prev_hash chain reveals the gap.
        """
        trail = _trail(4)
        del trail._entries[1]
        result = await _call(trail)
        assert result["intact"] is False

    @pytest.mark.asyncio
    async def test_a_break_is_logged_at_critical(self) -> None:
        """
        Nobody may be looking at the dashboard when the chain breaks.
        """
        from src.api.main import audit_chain_integrity

        with (
            patch("src.api.main.get_audit_trail", return_value=self._tampered()),
            patch("src.api.main.log") as mock_log,
        ):
            await audit_chain_integrity()
        mock_log.critical.assert_called_once()

    @pytest.mark.asyncio
    async def test_an_intact_chain_logs_nothing_critical(self) -> None:
        from src.api.main import audit_chain_integrity

        with (
            patch("src.api.main.get_audit_trail", return_value=_trail(3)),
            patch("src.api.main.log") as mock_log,
        ):
            await audit_chain_integrity()
        mock_log.critical.assert_not_called()


class TestDistinctFromDebugAudit:
    def test_it_reads_the_hash_chained_trail_not_the_trade_auditor(self) -> None:
        """
        /debug/audit reads TradeAuditor, a human-readable decision log with no
        integrity guarantee. Similar names, different modules — conflating
        them would leave the chain unverified again.
        """
        import inspect

        from src.api.main import audit_chain_integrity, debug_audit

        assert "get_audit_trail" in inspect.getsource(audit_chain_integrity)
        assert "get_auditor" in inspect.getsource(debug_audit)
        assert "get_auditor" not in inspect.getsource(audit_chain_integrity)


class TestVerifierItself:
    def test_verify_returns_the_sequence_not_the_index(self) -> None:
        """
        Sequence numbers are the stable identifier; a list index shifts the
        moment an entry is removed, which is one of the tamper cases.
        """
        trail = _trail(3)
        trail._entries[2] = replace(trail._entries[2], reason_code="rewritten")
        intact, first_broken = trail.verify_chain_integrity()
        assert intact is False
        assert first_broken == trail._entries[2].sequence

    def test_a_valid_trail_reports_no_broken_sequence(self) -> None:
        intact, first_broken = _trail(3).verify_chain_integrity()
        assert (intact, first_broken) == (True, None)


@pytest.mark.asyncio
async def test_the_endpoint_is_registered_and_read_only() -> None:
    """A verifier nobody can reach is the gap being closed."""
    from fastapi.routing import APIRoute

    from src.api.main import app

    routes = {r.path: r for r in app.routes if isinstance(r, APIRoute)}
    assert "/audit/integrity" in routes
    assert routes["/audit/integrity"].methods == {"GET"}
