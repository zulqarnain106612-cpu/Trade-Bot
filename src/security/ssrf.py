"""
SSRF protection for outbound requests.

API-004. The hazard the source document names:

> The application should not become a network proxy for an attacker.

A server that fetches a URL it was handed is a server that can be pointed at
`http://169.254.169.254/latest/meta-data/iam/security-credentials/` — the
cloud metadata service, which answers without authentication and returns
credentials. The same applies to `localhost`, the private RFC 1918 ranges, and
any internal hostname the attacker knows and you did not expect them to.

This project's outbound URLs come from configuration rather than from a
request body, which makes the attack harder but not the control unnecessary:
a configuration value is exactly what a compromised deployment pipeline
rewrites, and `DATA_PROVIDER_BASE_URL=http://169.254.169.254` is a one-line
change that turns a market-data fetch into a credential read.

The policy is **default-deny by address, not by name**. Blocking the string
"localhost" is defeated by `127.0.0.1`, by `0x7f.1`, by a DNS name that
resolves to loopback, and by `[::1]`. So the check resolves the host and
inspects the resulting IP addresses — *every* address, because a name that
resolves to one public and one private address must be refused rather than
accepted on the strength of the first answer.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

#: The only schemes an outbound fetch may use. `file://` reads the disk,
#: `gopher://` and `dict://` are classic SSRF pivots, and plain `http` is
#: permitted only because some venue sandboxes still do not offer TLS.
ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

#: Networks this application refuses by name rather than by classification.
#: Every one of these is *also* caught by one of `ipaddress`'s own checks --
#: the metadata addresses are link-local -- so the value here is not the
#: refusal, it is the reason attached to it. "link-local" sends an operator
#: looking at their network; "cloud instance metadata service" tells them what
#: was actually being reached for.
_NAMED_BLOCKS: tuple[tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, str], ...] = (
    (ipaddress.ip_network("169.254.169.254/32"), "cloud instance metadata service"),
    (ipaddress.ip_network("100.100.100.200/32"), "cloud instance metadata service"),
)


class SSRFError(ValueError):
    """Raised when an outbound URL is not a permitted destination."""


@dataclass(frozen=True)
class UrlVerdict:
    """Why one URL was allowed or refused."""

    url: str
    host: str
    addresses: tuple[str, ...]
    allowed: bool
    reason: str = ""


def _classify(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    """The reason this address is not routable to the public internet, or ""."""
    for network, reason in _NAMED_BLOCKS:
        if address.version == network.version and address in network:
            return reason
    # Ordered most specific first. `is_private` is true for the unspecified,
    # reserved, loopback and link-local ranges as well, so checking it early
    # would refuse 0.0.0.0 with the reason "private network" -- correct in
    # outcome and wrong in the log, which is where an operator finds out what
    # actually happened.
    if address.is_unspecified:
        return "unspecified"
    if address.is_loopback:
        return "loopback"
    if address.is_link_local:
        return "link-local"
    if address.is_multicast:
        return "multicast"
    if address.is_reserved:
        return "reserved"
    if address.is_private:
        return "private network"
    return ""


def _resolve(host: str) -> tuple[str, ...]:
    """
    Every address a host resolves to.

    Every, not the first: a name that resolves to one public and one private
    address must be refused rather than accepted on the strength of whichever
    answer came back first. That is the DNS-rebinding shape, and checking one
    address is how a guard passes it.
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError) as exc:
        raise SSRFError(f"outbound host {host!r} does not resolve: {exc}") from exc
    return tuple(dict.fromkeys(info[4][0] for info in infos))


def inspect_url(url: str, resolver=_resolve) -> UrlVerdict:
    """
    Decide whether `url` is a permitted outbound destination.

    `resolver` is injectable so the policy can be tested without DNS, and so
    a caller with its own resolution strategy (a pinned address, a proxy) can
    supply it. It is not a hook for skipping the check: whatever it returns
    is classified the same way.
    """
    parsed = urlparse(url)

    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        return UrlVerdict(
            url=url,
            host="",
            addresses=(),
            allowed=False,
            reason=(
                f"scheme {parsed.scheme!r} is not permitted; "
                f"allowed: {', '.join(sorted(ALLOWED_SCHEMES))}"
            ),
        )

    host = parsed.hostname or ""
    if not host:
        return UrlVerdict(url=url, host="", addresses=(), allowed=False, reason="no host in URL")

    try:
        addresses = resolver(host)
    except SSRFError as exc:
        return UrlVerdict(url=url, host=host, addresses=(), allowed=False, reason=str(exc))

    if not addresses:
        return UrlVerdict(
            url=url, host=host, addresses=(), allowed=False, reason="host resolved to no address"
        )

    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            return UrlVerdict(
                url=url,
                host=host,
                addresses=tuple(addresses),
                allowed=False,
                reason=f"unparseable address {raw!r}",
            )
        classification = _classify(address)
        if classification:
            return UrlVerdict(
                url=url,
                host=host,
                addresses=tuple(addresses),
                allowed=False,
                reason=f"{host} resolves to {raw} ({classification})",
            )

    return UrlVerdict(url=url, host=host, addresses=tuple(addresses), allowed=True)


def assert_outbound_url_allowed(url: str, resolver=_resolve) -> str:
    """
    Return `url` if it is a permitted destination, or raise.

    Raising rather than returning a flag at this call site is deliberate: an
    outbound fetch has exactly one safe response to "this destination is not
    allowed", and making the caller remember to check would put the control
    one forgotten `if` away from being absent.
    """
    verdict = inspect_url(url, resolver=resolver)
    if not verdict.allowed:
        raise SSRFError(f"refusing outbound request to {url!r}: {verdict.reason}")
    return url
