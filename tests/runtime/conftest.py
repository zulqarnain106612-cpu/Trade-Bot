"""Fixtures for the runtime-platform suite; helpers live in ``_support``."""

from __future__ import annotations

import pytest

from ._support import Platform, build_platform


@pytest.fixture
def platform() -> Platform:
    return build_platform()
