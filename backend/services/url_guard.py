"""Outbound URL safety guard (issue #17 — deep adversarial security audit).

Hive makes server-side HTTP requests to several classes of user-supplied URL:

  * BYOA ``endpoint_url`` (AgentCreate.endpoint_url) — delegation dispatch,
    health checks and skill discovery all contact it;
  * MCP server registry URLs (MCPServerCreate.url) and the OAuth metadata /
    token endpoints derived from them (routers/mcp_oauth.py);
  * delegation ``callback_url`` fields (DelegationRequest, TransactionCreate).

Without validation these turn the backend into an SSRF proxy: a malicious
user registers an agent whose endpoint is the cloud-metadata service
(169.254.169.254), loopback (127.0.0.1 / ::1 / localhost), or RFC1918 space,
then reads internal responses back through delegation logs, error messages
and skill-discovery output.

``validate_public_http_url`` fails closed:

  * scheme must be exactly ``http`` or ``https``;
  * no embedded userinfo (``http://user:pass@host``);
  * the hostname is resolved through ``socket.getaddrinfo`` — the SAME
    resolver the HTTP clients (aiohttp/httpx) use — and EVERY resulting
    address must be public. Resolving (instead of string-matching the host)
    neutralises alternate IP encodings such as ``2130706433``,
    ``0x7f000001`` or IPv4-mapped IPv6 ``::ffff:127.0.0.1``;
  * unresolvable hostnames are rejected.

Local development can relax the address checks with ``DEV_MODE=1`` or
``ALLOW_PRIVATE_URLS=1`` (self-hosted VPC installs where agents legitimately
live on private addresses). Production defaults to deny.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from typing import Iterable
from urllib.parse import urlparse


_ALLOWED_SCHEMES = ("http", "https")


def _allow_private() -> bool:
    """Whether private-address destinations are permitted (dev/VPC mode)."""
    return (
        os.getenv("DEV_MODE", "").lower() in ("1", "true", "yes")
        or os.getenv("ALLOW_PRIVATE_URLS", "").lower() in ("1", "true", "yes")
    )


def _is_forbidden_address(ip: ipaddress._BaseAddress) -> bool:
    """True if ``ip`` must never be contacted from server-side fetches."""
    # An IPv4-mapped IPv6 address (::ffff:127.0.0.1) is really an IPv4 target.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local          # incl. cloud metadata 169.254.169.254
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified         # 0.0.0.0 / ::
    )


def resolve_host_addresses(host: str) -> Iterable[ipaddress._BaseAddress]:
    """Resolve ``host`` exactly the way an HTTP client would."""
    results = socket.getaddrinfo(host, None)
    seen = set()
    for family, _type, _proto, _canonname, sockaddr in results:
        addr = ipaddress.ip_address(sockaddr[0])
        if addr not in seen:
            seen.add(addr)
            yield addr


def validate_public_http_url(url: str) -> str:
    """Validate that ``url`` is safe for server-side outbound requests.

    Returns the URL unchanged when safe; raises :class:`ValueError` with a
    specific, non-sensitive reason otherwise.
    """
    if not url or not isinstance(url, str):
        raise ValueError("URL is required")

    parsed = urlparse(url)

    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise ValueError("URL must use http:// or https://")

    if parsed.username or parsed.password:
        raise ValueError("URL must not embed credentials")

    host = parsed.hostname
    if not host:
        raise ValueError("URL has no hostname")

    # urlparse raises ValueError itself for ports outside 0..65535.
    if parsed.port is not None and parsed.port <= 0:
        raise ValueError("URL has an invalid port")

    host = host.rstrip(".")
    if not host:
        raise ValueError("URL has no hostname")

    if _allow_private():
        return url

    try:
        addresses = list(resolve_host_addresses(host))
    except (socket.gaierror, OSError, ValueError):
        raise ValueError(f"Cannot resolve URL hostname: {host}")

    if not addresses:
        raise ValueError(f"Cannot resolve URL hostname: {host}")

    for addr in addresses:
        if _is_forbidden_address(addr):
            raise ValueError(
                "URL must not point at private, loopback, link-local or "
                "reserved addresses"
            )

    return url
