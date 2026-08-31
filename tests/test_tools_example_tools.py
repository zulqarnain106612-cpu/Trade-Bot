"""Tests for tools/example_tools.py -- reference stubs showing the
orchestratable / direct-only split. Each stub deliberately raises
NotImplementedError; these tests confirm each is reachable and wired
under the right registry flag, not that it has real behavior yet."""

from __future__ import annotations

import pytest

from tools.example_tools import (
    delete_record,
    get_custom_budget,
    get_expenses,
    get_team_members,
    send_email,
)
from tools.registry import registry


@pytest.mark.parametrize(
    "coro_factory",
    [
        lambda: get_team_members("engineering"),
        lambda: get_expenses("emp-1", "Q1"),
        lambda: get_custom_budget("emp-1"),
    ],
)
async def test_orchestratable_stubs_raise_not_implemented(coro_factory):
    with pytest.raises(NotImplementedError):
        await coro_factory()


def test_send_email_raises_not_implemented():
    with pytest.raises(NotImplementedError):
        send_email("a@example.com", "subject", "body")


def test_delete_record_raises_not_implemented():
    with pytest.raises(NotImplementedError):
        delete_record("record-1")


def test_registered_flags_match_intended_safety_boundary():
    orchestratable_names = {"get_team_members", "get_expenses", "get_custom_budget"}
    direct_only_names = {"send_email", "delete_record"}
    for name in orchestratable_names:
        assert registry.get(name).orchestratable is True
    for name in direct_only_names:
        assert registry.get(name).orchestratable is False
