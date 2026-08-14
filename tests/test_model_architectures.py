"""Tests for the horizon model-architecture registry."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.models.architectures import (
    UnknownArchitectureError,
    available_architectures,
    build_architecture,
    horizon_architecture_names,
    horizon_model_names,
    is_training_modifier,
    load_horizon_architectures,
    resolve_architecture,
    validate_horizon_architectures,
)


_REPO_ROOT = Path(__file__).resolve().parents[1]


class TestResolution:
    def test_every_registered_name_resolves_to_a_class(self) -> None:
        for name in available_architectures():
            assert isinstance(resolve_architecture(name), type)

    def test_resolution_is_case_and_whitespace_insensitive(self) -> None:
        assert resolve_architecture("  TFT ") is resolve_architecture("tft")

    def test_unknown_name_raises_instead_of_defaulting(self) -> None:
        with pytest.raises(UnknownArchitectureError, match="unknown model architecture"):
            resolve_architecture("no_such_model")

    def test_build_returns_an_instance_of_the_resolved_class(self) -> None:
        cls = resolve_architecture("mlp")
        assert isinstance(build_architecture("mlp"), cls)

    def test_build_forwards_constructor_overrides(self) -> None:
        head = build_architecture("mlp", hidden_dim=32)
        assert any(getattr(m, "out_features", None) == 32 for m in head.modules())


class TestTrainingModifiers:
    def test_maml_is_a_modifier_not_an_architecture(self) -> None:
        assert is_training_modifier("maml")
        assert "maml" not in available_architectures()

    def test_a_modifier_never_resolves_to_a_head_class(self) -> None:
        with pytest.raises(UnknownArchitectureError):
            resolve_architecture("maml")

    def test_modifiers_validate_but_are_excluded_from_architectures(self) -> None:
        config = {"horizons": {"h8": {"models": ["maml", "mlp"]}}}
        assert validate_horizon_architectures(config) == {"h8": ["maml", "mlp"]}
        assert horizon_architecture_names(config) == {"h8": ["mlp"]}

    def test_every_shipped_architecture_name_is_buildable(self) -> None:
        config = yaml.safe_load((_REPO_ROOT / "config" / "horizons.yaml").read_text())
        for names in horizon_architecture_names(config).values():
            for name in names:
                assert isinstance(resolve_architecture(name), type)


class TestHorizonConfig:
    def test_names_are_collected_per_horizon(self) -> None:
        config = {"horizons": {"h1": {"models": ["cnn", "tcn"]}, "h2": {"models": ["lstm"]}}}
        assert horizon_model_names(config) == {"h1": ["cnn", "tcn"], "h2": ["lstm"]}

    def test_missing_or_null_sections_yield_empty_lists(self) -> None:
        assert horizon_model_names({}) == {}
        assert horizon_model_names({"horizons": {"h1": None}}) == {"h1": []}

    def test_validation_passes_for_known_names(self) -> None:
        config = {"horizons": {"h1": {"models": ["cnn", "tcn"]}}}
        assert validate_horizon_architectures(config) == {"h1": ["cnn", "tcn"]}

    def test_validation_reports_the_offending_horizon_and_model(self) -> None:
        config = {"horizons": {"h1": {"models": ["cnn", "bogus"]}}}
        with pytest.raises(UnknownArchitectureError, match="h1:bogus"):
            validate_horizon_architectures(config)

    def test_shipped_horizons_config_only_names_known_architectures(self) -> None:
        names = load_horizon_architectures(_REPO_ROOT / "config" / "horizons.yaml")
        assert names, "config/horizons.yaml declared no horizons"

    def test_missing_config_file_is_not_an_error(self, tmp_path: Path) -> None:
        assert load_horizon_architectures(tmp_path / "absent.yaml") == {}

    def test_existing_config_with_a_bad_name_is_an_error(self, tmp_path: Path) -> None:
        path = tmp_path / "horizons.yaml"
        path.write_text(yaml.safe_dump({"horizons": {"h1": {"models": ["bogus"]}}}))
        with pytest.raises(UnknownArchitectureError):
            load_horizon_architectures(path)
