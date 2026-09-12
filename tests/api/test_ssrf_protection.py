"""
API-004 — outbound URL handling is SSRF-resistant.

> The application should not become a network proxy for an attacker.

The address that matters most is `169.254.169.254`. It answers without
authentication and returns cloud credentials, which makes "fetch this URL for
me" into "read my IAM role" in one request.

The policy is **default-deny by address, not by name**, and the tests are
organised around the ways a name-based block is defeated: `127.0.0.1` instead
of `localhost`, `[::1]` instead of either, a public name that resolves to a
private address, and a name that resolves to two addresses where only one is
public.

The resolver is injected throughout, so the policy is tested without DNS and
without the results depending on whoever's network the suite runs on.
"""

from __future__ import annotations

import pytest

from src.api.ssrf import (
    ALLOWED_SCHEMES,
    SSRFError,
    assert_outbound_url_allowed,
    inspect_url,
)


def resolves_to(*addresses: str):
    """A resolver stub returning fixed addresses for any host."""

    def _resolve(_host: str) -> tuple[str, ...]:
        return addresses

    return _resolve


PUBLIC = resolves_to("93.184.216.34")


class TestPermittedDestinations:
    def test_a_public_https_url_is_allowed(self):
        verdict = inspect_url("https://api.example.com/v1/metrics", resolver=PUBLIC)
        assert verdict.allowed
        assert verdict.reason == ""

    def test_plain_http_is_allowed(self):
        # Permitted only because some venue sandboxes still do not offer TLS.
        # TLS enforcement is SECR-006's job, not this guard's.
        assert inspect_url("http://api.example.com", resolver=PUBLIC).allowed

    def test_a_port_and_path_do_not_matter(self):
        assert inspect_url("https://api.example.com:8443/a/b?c=d", resolver=PUBLIC).allowed

    def test_the_verdict_records_what_it_resolved(self):
        verdict = inspect_url("https://api.example.com", resolver=PUBLIC)
        assert verdict.host == "api.example.com"
        assert verdict.addresses == ("93.184.216.34",)


class TestTheMetadataService:
    @pytest.mark.parametrize(
        "address",
        ["169.254.169.254", "100.100.100.200"],
        ids=["aws-gcp-azure", "alibaba"],
    )
    def test_a_metadata_address_is_refused(self, address):
        verdict = inspect_url("http://metadata.internal/", resolver=resolves_to(address))
        assert not verdict.allowed
        assert "metadata" in verdict.reason

    def test_it_is_refused_even_when_reached_by_literal_address(self):
        verdict = inspect_url(
            "http://169.254.169.254/latest/meta-data/",
            resolver=resolves_to("169.254.169.254"),
        )
        assert not verdict.allowed

    def test_a_public_looking_name_that_resolves_there_is_refused(self):
        # The whole point of resolving before deciding.
        verdict = inspect_url(
            "https://totally-legitimate-api.example.com/",
            resolver=resolves_to("169.254.169.254"),
        )
        assert not verdict.allowed
        assert "metadata" in verdict.reason


class TestPrivateAndLocalAddresses:
    @pytest.mark.parametrize(
        ("address", "fragment"),
        [
            ("127.0.0.1", "loopback"),
            ("127.1.2.3", "loopback"),
            ("::1", "loopback"),
            ("10.0.0.5", "private network"),
            ("172.16.3.4", "private network"),
            ("192.168.1.1", "private network"),
            ("fd00::1", "private network"),
            ("169.254.1.1", "link-local"),
            ("fe80::1", "link-local"),
            ("0.0.0.0", "unspecified"),
            ("224.0.0.1", "multicast"),
            ("240.0.0.1", "reserved"),
        ],
    )
    def test_each_is_refused_with_a_reason(self, address, fragment):
        verdict = inspect_url("http://anything/", resolver=resolves_to(address))
        assert not verdict.allowed
        assert fragment in verdict.reason

    def test_localhost_by_name_is_refused(self):
        assert not inspect_url("http://localhost:8000/", resolver=resolves_to("127.0.0.1")).allowed

    def test_the_reason_names_the_most_specific_classification(self):
        # `is_private` is true for the unspecified, reserved, loopback and
        # link-local ranges too, so an early private check would refuse
        # 0.0.0.0 with the reason "private network" -- correct in outcome,
        # wrong in the log, which is where an operator finds out what
        # actually happened.
        assert "unspecified" in inspect_url("http://x/", resolver=resolves_to("0.0.0.0")).reason
        assert "loopback" in inspect_url("http://x/", resolver=resolves_to("127.0.0.1")).reason

    def test_a_named_block_wins_over_the_generic_classification(self):
        # 169.254.169.254 is link-local, so it would be refused either way.
        # The named block exists for the *reason*: "link-local" sends an
        # operator looking at their network, "cloud instance metadata
        # service" tells them what was actually being reached for.
        verdict = inspect_url("http://x/", resolver=resolves_to("169.254.169.254"))
        assert "metadata" in verdict.reason
        assert "link-local" not in verdict.reason

    def test_an_ipv6_address_is_not_matched_against_an_ipv4_block(self):
        # `address in network` raises TypeError across families, so the
        # version guard is what keeps a v6 answer from taking down the check.
        assert not inspect_url("http://x/", resolver=resolves_to("::1")).allowed

    def test_the_real_resolver_returns_addresses_for_a_name_that_exists(self):
        # The success path, which the failure tests below cannot reach.
        from src.api.ssrf import _resolve

        addresses = _resolve("localhost")
        assert addresses
        assert all(isinstance(a, str) for a in addresses)

    def test_the_real_resolver_refuses_a_name_that_does_not_exist(self):
        # The one path that needs real DNS. `.invalid` is reserved by RFC
        # 2606 precisely so it can never resolve.
        from src.api.ssrf import SSRFError, _resolve

        with pytest.raises(SSRFError, match="does not resolve"):
            _resolve("this-name-cannot-exist.invalid")

    def test_the_real_resolver_refuses_an_unencodable_name(self):
        from src.api.ssrf import SSRFError, _resolve

        with pytest.raises(SSRFError, match="does not resolve"):
            _resolve("x" * 100 + "\udc80")

    def test_an_obfuscated_loopback_literal_is_refused(self):
        # `0x7f.1` and friends are why the check resolves rather than
        # pattern-matching the string.
        assert not inspect_url("http://0x7f.1/", resolver=resolves_to("127.0.0.1")).allowed


class TestEveryResolvedAddressIsChecked:
    def test_one_private_address_among_public_ones_refuses(self):
        # The DNS-rebinding shape. Checking only the first answer is exactly
        # how a guard passes it.
        verdict = inspect_url(
            "https://api.example.com/",
            resolver=resolves_to("93.184.216.34", "10.0.0.5"),
        )
        assert not verdict.allowed
        assert "private network" in verdict.reason

    def test_the_order_of_the_answers_does_not_matter(self):
        verdict = inspect_url(
            "https://api.example.com/",
            resolver=resolves_to("10.0.0.5", "93.184.216.34"),
        )
        assert not verdict.allowed

    def test_all_public_addresses_are_allowed(self):
        assert inspect_url(
            "https://api.example.com/", resolver=resolves_to("93.184.216.34", "1.1.1.1")
        ).allowed


class TestSchemesAndMalformedUrls:
    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "gopher://example.com/",
            "dict://example.com/",
            "ftp://example.com/",
            "jar:http://example.com!/",
            "data:text/plain,hello",
        ],
    )
    def test_a_non_http_scheme_is_refused(self, url):
        verdict = inspect_url(url, resolver=PUBLIC)
        assert not verdict.allowed
        assert "scheme" in verdict.reason

    def test_only_http_and_https_are_permitted(self):
        assert {"http", "https"} == ALLOWED_SCHEMES

    @pytest.mark.parametrize("url", ["", "not-a-url", "https://", "//example.com/"])
    def test_a_malformed_url_is_refused(self, url):
        assert not inspect_url(url, resolver=PUBLIC).allowed

    def test_a_host_that_does_not_resolve_is_refused(self):
        def _boom(_host: str):
            raise SSRFError("no such host")

        verdict = inspect_url("https://nope.invalid/", resolver=_boom)
        assert not verdict.allowed
        assert "no such host" in verdict.reason

    def test_a_host_resolving_to_nothing_is_refused(self):
        verdict = inspect_url("https://api.example.com/", resolver=resolves_to())
        assert not verdict.allowed
        assert "no address" in verdict.reason

    def test_an_unparseable_address_is_refused(self):
        verdict = inspect_url("https://api.example.com/", resolver=resolves_to("not-an-ip"))
        assert not verdict.allowed
        assert "unparseable" in verdict.reason


class TestTheRaisingHelper:
    def test_it_returns_the_url_when_allowed(self):
        url = "https://api.example.com/v1"
        assert assert_outbound_url_allowed(url, resolver=PUBLIC) == url

    def test_it_raises_when_refused(self):
        with pytest.raises(SSRFError, match="refusing outbound request"):
            assert_outbound_url_allowed(
                "http://169.254.169.254/", resolver=resolves_to("169.254.169.254")
            )

    def test_the_message_names_the_url_and_the_reason(self):
        with pytest.raises(SSRFError) as excinfo:
            assert_outbound_url_allowed("http://internal/", resolver=resolves_to("10.0.0.5"))
        assert "10.0.0.5" in str(excinfo.value)
        assert "private network" in str(excinfo.value)


class TestTheGuardIsWired:
    def test_the_intelligence_client_checks_its_base_url(self):
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[2] / "src" / "intelligence" / "client.py"
        ).read_text(encoding="utf-8")
        assert "assert_outbound_url_allowed" in source

    def test_a_poisoned_base_url_is_refused_at_use(self, monkeypatch):
        # The realistic attack on a configuration-driven URL: a compromised
        # deployment pipeline rewrites one environment variable.
        from src.api import ssrf

        monkeypatch.setattr(ssrf, "_resolve", resolves_to("169.254.169.254"))
        with pytest.raises(SSRFError):
            ssrf.assert_outbound_url_allowed("https://glassnode.example.com/v1")
