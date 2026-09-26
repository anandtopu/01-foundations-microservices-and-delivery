"""SSRF guard for shipper-supplied webhook URLs (spec M7, section 9).

`assert_public_https` is the spec's code, verbatim: HTTPS only, and every address the host resolves
to must be globally routable (no RFC 1918, loopback, link-local incl. 169.254.169.254, ULA, ...).
Called at subscription time AND before every delivery attempt, because DNS answers change.

Known gap (spec): a DNS-rebinding window remains between this resolution and the connection. In
production, close it with an allow-listing egress proxy that resolves for itself.

`guard` adds one lab-only escape hatch (ARCHITECTURE difference 37): hostnames listed in
WEBHOOK_DEV_ALLOW_HOSTS (exact, case-insensitive match; empty by default, never set in production)
skip the address check so the dispatcher can reach the local webhook-sink. HTTPS is still enforced.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Collection
from urllib.parse import urlsplit


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


async def guard(url: str, dev_allow_hosts: Collection[str] = ()) -> None:
    """assert_public_https, except for the lab's explicitly allow-listed hostnames."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("webhook URL must be https")
    if parts.hostname.lower() in {h.lower() for h in dev_allow_hosts}:
        return  # lab only: the local sink (difference 37)
    await assert_public_https(url)
