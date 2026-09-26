"""SSRF guard for shipper-supplied webhook URLs (spec M7, section 9).

`assert_public_https` is the spec's code, verbatim: HTTPS only, and every address the host resolves
to must be globally routable (no RFC 1918, loopback, link-local incl. 169.254.169.254, ULA, ...).
Called at subscription time AND before every delivery attempt, because DNS answers change.

Known gap (spec; ARCHITECTURE difference 44): a DNS-rebinding window remains between this
resolution and the client's own connect. A hostile name server can answer "public" here and
"internal" a moment later: TLS verification stops the request body, but the connection itself
reaches the internal address (blind port probing). Per-attempt checks only defeat SLOW DNS changes.
In production, close it with an allow-listing egress proxy that resolves for itself.

`guard` adds one lab-only escape hatch (ARCHITECTURE difference 37): hostnames listed in
WEBHOOK_DEV_ALLOW_HOSTS (exact, case-insensitive match; empty by default, never set in production)
skip the address check so the dispatcher can reach the local webhook-sink. HTTPS is still enforced.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Collection
from urllib.parse import urlsplit

import httpx


async def assert_public_https(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("webhook URL must be https")
    infos = await asyncio.get_running_loop().getaddrinfo(
        parts.hostname, parts.port or 443, type=socket.SOCK_STREAM
    )
    for *_, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if not ip.is_global:  # RFC 1918, loopback, link-local incl. 169.254.169.254, ULA
            raise ValueError(f"webhook target resolves to non-public address {ip}")


# Addresses Python's `is_global` calls global but that reach an IPv4 address we must also check
# (PR #5 review): NAT64 prefixes translate to the embedded IPv4 on IPv6-only subnets (AWS included),
# and the deprecated IPv4-compatible ::a.b.c.d form. (::ffff:a.b.c.d is already refused.)
NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))
IPV4_COMPATIBLE = ipaddress.ip_network("::/96")


def embedded_ipv4_is_private(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if not isinstance(ip, ipaddress.IPv6Address):
        return False
    if ip in IPV4_COMPATIBLE:
        return True  # never a legitimate webhook target
    if ip.ipv4_mapped is not None:
        return not ip.ipv4_mapped.is_global
    if any(ip in net for net in NAT64):
        return not ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF).is_global
    return False


def check_url_syntax(url: str) -> None:
    """Refuse what urlsplit silently repairs but the HTTP client rejects or reads differently:
    control characters and whitespace (urlsplit drops \t\n\r, so the guard would check one URL
    and httpx fail on another), and userinfo (sent as Basic auth). PR #5 review."""
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        raise ValueError("webhook URL contains whitespace or control characters")
    parsed = httpx.URL(url)  # the dispatcher's own parser: raises httpx.InvalidURL
    if parsed.scheme != "https" or not parsed.host:
        raise ValueError("webhook URL must be https")
    if parsed.userinfo:
        raise ValueError("webhook URL must not contain credentials")


async def guard(url: str, dev_allow_hosts: Collection[str] = ()) -> None:
    """assert_public_https, plus the URL syntax and embedded-IPv4 checks, except for the lab's
    explicitly allow-listed hostnames."""
    try:
        check_url_syntax(url)
    except httpx.InvalidURL as exc:
        raise ValueError(f"invalid webhook URL: {exc}") from exc
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("webhook URL must be https")
    if parts.hostname.lower() in {h.lower() for h in dev_allow_hosts}:
        return  # lab only: the local sink (difference 37)
    await assert_public_https(url)
    infos = await asyncio.get_running_loop().getaddrinfo(
        parts.hostname, parts.port or 443, type=socket.SOCK_STREAM
    )
    for *_, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if embedded_ipv4_is_private(ip):
            raise ValueError(f"webhook target resolves to non-public address {ip}")
