"""Tests for src/api/middleware.py — CORS origin validation."""

import pytest

from src.api.middleware import validate_cors_config


class TestValidateCorsConfig:
    def test_valid_https_origin_passes(self):
        validate_cors_config(["https://example.com"], allow_credentials=False)

    def test_valid_http_origin_with_port_passes(self):
        validate_cors_config(["http://localhost:5173"], allow_credentials=True)

    def test_multiple_valid_origins_pass(self):
        validate_cors_config(
            ["https://example.com", "http://localhost:5173"], allow_credentials=True
        )

    def test_wildcard_with_credentials_rejected(self):
        with pytest.raises(RuntimeError, match="allow_credentials"):
            validate_cors_config(["*"], allow_credentials=True)

    def test_wildcard_without_credentials_rejected_by_format(self):
        # "*" isn't a valid http(s)://host pattern either way.
        with pytest.raises(RuntimeError, match="does not match"):
            validate_cors_config(["*"], allow_credentials=False)

    def test_null_origin_rejected(self):
        with pytest.raises(RuntimeError, match="null"):
            validate_cors_config(["null"], allow_credentials=False)

    def test_null_origin_case_insensitive_rejected(self):
        with pytest.raises(RuntimeError, match="null"):
            validate_cors_config(["NULL"], allow_credentials=False)

    def test_bare_ip_without_scheme_rejected(self):
        with pytest.raises(RuntimeError, match="does not match"):
            validate_cors_config(["192.168.1.1"], allow_credentials=False)

    def test_schemeless_origin_rejected(self):
        with pytest.raises(RuntimeError, match="does not match"):
            validate_cors_config(["example.com"], allow_credentials=False)

    def test_ftp_scheme_rejected(self):
        with pytest.raises(RuntimeError, match="does not match"):
            validate_cors_config(["ftp://example.com"], allow_credentials=False)

    def test_empty_origins_list_passes(self):
        validate_cors_config([], allow_credentials=False)
