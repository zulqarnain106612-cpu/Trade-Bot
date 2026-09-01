"""Tests for tools/registry.py -- the PTC orchestratable/direct-only tool registry."""

from __future__ import annotations

import pytest

from tools.registry import ToolRegistry, ToolSpec, registry, tool


def _sync_fn(x: int) -> int:
    """A sync tool."""
    return x + 1


async def _async_fn(x: int) -> int:
    return x + 2


def test_toolspec_detects_sync_function():
    spec = ToolSpec(name="s", fn=_sync_fn, orchestratable=True, description="d")
    assert spec.is_async is False


def test_toolspec_detects_async_function():
    spec = ToolSpec(name="a", fn=_async_fn, orchestratable=True, description="d")
    assert spec.is_async is True


def test_register_and_get():
    reg = ToolRegistry()
    reg.register("sync_tool", _sync_fn, orchestratable=True)
    spec = reg.get("sync_tool")
    assert spec.fn is _sync_fn
    assert spec.orchestratable is True


def test_register_defaults_description_to_docstring():
    reg = ToolRegistry()
    reg.register("sync_tool", _sync_fn, orchestratable=True)
    assert reg.get("sync_tool").description == "A sync tool."


def test_register_duplicate_name_raises():
    reg = ToolRegistry()
    reg.register("dup", _sync_fn, orchestratable=True)
    with pytest.raises(ValueError, match="already registered"):
        reg.register("dup", _sync_fn, orchestratable=True)


def test_get_unknown_tool_raises_key_error_listing_registered():
    reg = ToolRegistry()
    reg.register("known", _sync_fn, orchestratable=True)
    with pytest.raises(KeyError, match="Unknown tool 'missing'"):
        reg.get("missing")


def test_list_orchestratable_and_direct_only():
    reg = ToolRegistry()
    reg.register("batchable", _sync_fn, orchestratable=True)
    reg.register("one_at_a_time", _async_fn, orchestratable=False)
    assert set(reg.list_orchestratable()) == {"batchable"}
    assert set(reg.list_direct_only()) == {"one_at_a_time"}


def test_assert_orchestratable_returns_spec_when_true():
    reg = ToolRegistry()
    reg.register("batchable", _sync_fn, orchestratable=True)
    assert reg.assert_orchestratable("batchable").name == "batchable"


def test_assert_orchestratable_raises_permission_error_when_false():
    reg = ToolRegistry()
    reg.register("dangerous", _sync_fn, orchestratable=False)
    with pytest.raises(PermissionError, match="not orchestratable=True"):
        reg.assert_orchestratable("dangerous")


def test_namespace_maps_names_to_callables():
    reg = ToolRegistry()
    reg.register("batchable", _sync_fn, orchestratable=True)
    reg.register("direct_only", _async_fn, orchestratable=False)
    ns = reg.namespace()
    assert ns == {"batchable": _sync_fn}


def test_tool_decorator_registers_into_global_registry_and_preserves_call_behavior():
    @tool("__test_tool_decorator_unique__", orchestratable=True, description="desc")
    def _decorated(x: int) -> int:
        return x * 2

    spec = registry.get("__test_tool_decorator_unique__")
    assert spec.orchestratable is True
    assert spec.description == "desc"
    assert _decorated(21) == 42
