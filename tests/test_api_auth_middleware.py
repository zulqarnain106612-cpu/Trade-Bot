"""
Pure-logic tests for src/api/auth.py and src/api/middleware.py.

Tests input validation and CORS config enforcement without any HTTP server
or exchange connectivity.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from src.api.auth import _MIN_KEY_LENGTH, _get_configured_key, verify_api_key
from src.api.middleware import validate_cors_config


# ---------------------------------------------------------------------------
# src/api/middleware.py — validate_cors_config
# ---------------------------------------------------------------------------


class TestValidateCorsConfig:
    def test_valid_https_origin_passes(self) -> None:
        validate_cors_config(["https://example.com"], allow_credentials=False)

    def test_valid_http_origin_with_port(self) -> None:
        validate_cors_config(["http://localhost:3000"], allow_credentials=True)

    def test_wildcard_always_raises(self) -> None:
        # '*' fails the hostname regex regardless of allow_credentials
        with pytest.raises(RuntimeError, match="does not match"):
            validate_cors_config(["*"], allow_credentials=False)

    def test_wildcard_with_credentials_raises(self) -> None:
        with pytest.raises(RuntimeError, match="allow_credentials"):
            validate_cors_config(["*"], allow_credentials=True)

    def test_null_origin_raises(self) -> None:
        with pytest.raises(RuntimeError, match="null"):
            validate_cors_config(["null"], allow_credentials=False)

    def test_null_origin_case_insensitive(self) -> None:
        with pytest.raises(RuntimeError, match="null"):
            validate_cors_config(["NULL"], allow_credentials=False)

    def test_bare_ip_raises(self) -> None:
        with pytest.raises(RuntimeError, match="does not match"):
            validate_cors_config(["192.168.1.1:8080"], allow_credentials=False)

    def test_schemeless_raises(self) -> None:
        with pytest.raises(RuntimeError, match="does not match"):
            validate_cors_config(["example.com"], allow_credentials=False)

    def test_multiple_valid_origins(self) -> None:
        validate_cors_config(
            ["https://app.example.com", "https://api.example.com"],
            allow_credentials=True,
        )

    def test_empty_origins_list_is_valid(self) -> None:
        validate_cors_config([], allow_credentials=True)

    def test_origin_with_subdomain(self) -> None:
        validate_cors_config(["https://sub.domain.example.co.uk"], allow_credentials=False)

    def test_origin_with_port_number(self) -> None:
        validate_cors_config(["https://api.example.com:8443"], allow_credentials=False)

    def test_mixed_valid_and_invalid_raises_on_first_invalid(self) -> None:
        with pytest.raises(RuntimeError):
            validate_cors_config(
                ["https://good.example.com", "bad-origin"],
                allow_credentials=False,
            )


# ---------------------------------------------------------------------------
# src/api/auth.py — _get_configured_key
# ---------------------------------------------------------------------------


class TestGetConfiguredKey:
    def test_missing_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("API_SECRET_KEY", raising=False)
        with pytest.raises(RuntimeError, match="not set"):
            _get_configured_key()

    def test_empty_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("API_SECRET_KEY", "")
        with pytest.raises(RuntimeError, match="not set"):
            _get_configured_key()

    def test_short_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("API_SECRET_KEY", "tooshort")
        with pytest.raises(RuntimeError, match="too short"):
            _get_configured_key()

    def test_exactly_min_length_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        key = "a" * _MIN_KEY_LENGTH
        monkeypatch.setenv("API_SECRET_KEY", key)
        assert _get_configured_key() == key

    def test_long_key_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        key = "x" * 64
        monkeypatch.setenv("API_SECRET_KEY", key)
        assert _get_configured_key() == key

    def test_key_stripped_of_whitespace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        key = "a" * _MIN_KEY_LENGTH
        monkeypatch.setenv("API_SECRET_KEY", f"  {key}  ")
        assert _get_configured_key() == key


class TestVerifyApiKey:
    def test_valid_key_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        key = "a" * _MIN_KEY_LENGTH
        monkeypatch.setenv("API_SECRET_KEY", key)
        verify_api_key(key)  # should not raise

    def test_wrong_key_raises_401(self, monkeypatch: pytest.MonkeyPatch) -> None:
        key = "a" * _MIN_KEY_LENGTH
        monkeypatch.setenv("API_SECRET_KEY", key)
        with pytest.raises(HTTPException) as exc_info:
            verify_api_key("wrong" * 10)
        assert exc_info.value.status_code == 401

    def test_none_key_raises_401(self, monkeypatch: pytest.MonkeyPatch) -> None:
        key = "a" * _MIN_KEY_LENGTH
        monkeypatch.setenv("API_SECRET_KEY", key)
        with pytest.raises(HTTPException) as exc_info:
            verify_api_key(None)
        assert exc_info.value.status_code == 401

    def test_server_unconfigured_raises_503(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("API_SECRET_KEY", raising=False)
        with pytest.raises(HTTPException) as exc_info:
            verify_api_key("anything")
        assert exc_info.value.status_code == 503

    def test_min_key_length_constant(self) -> None:
        assert _MIN_KEY_LENGTH >= 32  # 256-bit minimum
