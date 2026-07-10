"""
Phase 7: verifies the live-promotion capability's default posture and
that it is driven by config, not a runtime toggle.
"""

from __future__ import annotations

from src.config import SelfTuningSettings
from src.tuning import state as tuning_state


def test_shadow_mode_defaults_true() -> None:
    assert SelfTuningSettings().shadow_mode is True


def test_shadow_mode_can_be_overridden_via_settings_only() -> None:
    live_settings = SelfTuningSettings(shadow_mode=False)
    assert live_settings.shadow_mode is False
    # No API endpoint exists to flip this at runtime -- confirmed by
    # absence from the Phase 6 endpoint set (only pause/resume/rollback
    # exist; see src/api/main.py's /self-tuning/* routes).


def test_module_singleton_runner_defaults_to_shadow_mode() -> None:
    """The process-wide TuningRunner singleton must start in shadow_mode
    in the standard test/dev environment (SELF_TUNING_SHADOW_MODE unset),
    so importing src.tuning.state never accidentally enables live
    promotion."""
    assert tuning_state.runner._shadow_mode is True  # -- test-only introspection
    assert tuning_state._settings.enabled is False
