"""Issue #17 — SSRF guard regression tests (deep adversarial security audit).

Hive contacts several classes of user-supplied URL from the backend:
BYOA agent ``endpoint_url`` (delegation dispatch, health checks, skill
discovery), MCP server registry URLs + derived OAuth endpoints, and
delegation ``callback_url`` fields. All of them must be forced through
``services.url_guard.validate_public_http_url``, which rejects:

  * non-http(s) schemes (file:, gopher:, ftp:, ...)
  * embedded credentials (http://user:pass@host)
  * hostnames resolving to loopback / private / link-local / reserved /
    multicast / unspecified addresses — including alternate IP encodings
    (decimal 2130706433, hex 0x7f000001, IPv4-mapped ::ffff:127.0.0.1)
  * unresolvable hostnames

The schema-level tests use the real resolver; the guard is bypassed for
loopback names only via the explicit DEV_MODE / ALLOW_PRIVATE_URLS escape
hatch, which these tests never set.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."
))
# routers.mcp / routers.delegation import auth, which requires SECRET_KEY at
# import time; provide a deterministic test-only value BEFORE those imports.
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-url-guard-tests")
from services.url_guard import (  # noqa: E402
    validate_public_http_url,
    _is_forbidden_address,
)

import ipaddress  # noqa: E402


def _fake_resolver(mapping):
    """Return a getaddrinfo stand-in resolving names via ``mapping``.

    Names not present in the mapping raise socket.gaierror like a real
    NXDOMAIN. The sockaddr layout matches socket.getaddrinfo's 4-tuple.
    """
    import socket

    def _getaddrinfo(host, port, *args, **kwargs):
        if host not in mapping:
            raise socket.gaierror(
                socket.EAI_NONAME, "Name or service not known"
            )
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (mapping[host], port or 0))]

    return _getaddrinfo


class TestValidatePublicHttpUrlSchemeAndSyntax(unittest.TestCase):
    def test_rejects_non_http_schemes(self):
        for url in (
            "file:///etc/passwd",
            "gopher://127.0.0.1:70/",
            "ftp://example.com/",
            "javascript:alert(1)",
            "data:text/html,hello",
        ):
            with self.assertRaises(ValueError, msg=url):
                validate_public_http_url(url)

    def test_rejects_missing_hostname(self):
        with self.assertRaises(ValueError):
            validate_public_http_url("http:///path")

    def test_rejects_empty_and_none(self):
        with self.assertRaises(ValueError):
            validate_public_http_url("")
        with self.assertRaises(ValueError):
            validate_public_http_url(None)

    def test_rejects_embedded_credentials(self):
        with mock.patch(
            "services.url_guard.socket.getaddrinfo",
            _fake_resolver({"example.com": "93.184.216.34"}),
        ):
            with self.assertRaises(ValueError):
                validate_public_http_url("http://user:pass@example.com/")

    def test_rejects_unresolvable_host(self):
        with mock.patch(
            "services.url_guard.socket.getaddrinfo",
            _fake_resolver({}),
        ):
            with self.assertRaises(ValueError):
                validate_public_http_url("http://does-not-exist.invalid/")

    def test_https_public_url_is_accepted(self):
        with mock.patch(
            "services.url_guard.socket.getaddrinfo",
            _fake_resolver({"example.com": "93.184.216.34"}),
        ):
            self.assertEqual(
                validate_public_http_url("https://example.com/invoke"),
                "https://example.com/invoke",
            )


class TestValidatePublicHttpUrlAddresses(unittest.TestCase):
    """Literal and resolved addresses that must always be blocked."""

    _CASES = (
        "http://127.0.0.1:8000/invoke",
        "http://[::1]:9000/",
        "http://0.0.0.0/",
        "http://169.254.169.254/latest/meta-data/",   # cloud metadata
        "http://10.0.0.5/",
        "http://172.16.0.9/",
        "http://192.168.1.10/",
        "http://[fe80::1]/",
        "http://[fc00::1]/",
        "http://2130706433/",                         # 127.0.0.1 as decimal
        "http://0x7f000001/",                         # 127.0.0.1 as hex
    )

    def test_rejects_forbidden_addresses(self):
        for url in self._CASES:
            with self.assertRaises(ValueError, msg=url):
                validate_public_http_url(url)

    def test_hostname_resolving_to_loopback_is_blocked(self):
        # The classic DNS-rebinding/SSRF bypass: a name that resolves to
        # 127.0.0.1. The guard must resolve before allowing.
        with mock.patch(
            "services.url_guard.socket.getaddrinfo",
            _fake_resolver({"evil.example.com": "127.0.0.1"}),
        ):
            with self.assertRaises(ValueError):
                validate_public_http_url("http://evil.example.com/")

    def test_hostname_resolving_to_metadata_ip_is_blocked(self):
        with mock.patch(
            "services.url_guard.socket.getaddrinfo",
            _fake_resolver({"rebind.example.com": "169.254.169.254"}),
        ):
            with self.assertRaises(ValueError):
                validate_public_http_url("http://rebind.example.com/")

    def test_multi_address_host_blocked_if_any_address_is_private(self):
        def _multi(host, port, *a, **kw):
            import socket
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", port or 0)),
            ]
        with mock.patch("services.url_guard.socket.getaddrinfo", _multi):
            with self.assertRaises(ValueError):
                validate_public_http_url("http://mixed.example.com/")

    def test_ipv4_mapped_ipv6_loopback_is_blocked(self):
        self.assertTrue(_is_forbidden_address(
            ipaddress.ip_address("::ffff:127.0.0.1")))
        self.assertTrue(_is_forbidden_address(
            ipaddress.ip_address("::ffff:10.0.0.1")))

    def test_dev_mode_allows_private_urls(self):
        old = os.environ.get("DEV_MODE")
        os.environ["DEV_MODE"] = "1"
        try:
            self.assertEqual(
                validate_public_http_url("http://127.0.0.1:8000/"),
                "http://127.0.0.1:8000/",
            )
        finally:
            if old is None:
                os.environ.pop("DEV_MODE", None)
            else:
                os.environ["DEV_MODE"] = old


class TestSchemaLevelSsrfGuards(unittest.TestCase):
    """User-facing request schemas must route through the guard."""

    def test_agent_create_endpoint_url_blocks_loopback(self):
        from schemas import AgentCreate
        with self.assertRaises(Exception):
            AgentCreate(
                name="evil", agent_type="external",
                endpoint_url="http://169.254.169.254/latest/meta-data/",
            )

    def test_agent_create_endpoint_url_blocks_metadata_name(self):
        from schemas import AgentCreate
        with self.assertRaises(Exception):
            AgentCreate(
                name="evil", agent_type="external",
                endpoint_url="http://127.0.0.1:8000/invoke",
            )

    def test_agent_create_accepts_public_endpoint(self):
        from schemas import AgentCreate
        with mock.patch(
            "services.url_guard.socket.getaddrinfo",
            _fake_resolver({"byoa.example.com": "93.184.216.34"}),
        ):
            agent = AgentCreate(
                name="ok", agent_type="external",
                endpoint_url="https://byoa.example.com/invoke",
            )
        self.assertEqual(agent.endpoint_url, "https://byoa.example.com/invoke")

    def test_delegation_request_callback_url_blocks_private(self):
        from schemas import DelegationRequest
        base = dict(
            target_agent_id="a1", task_description="t", max_tokens=1,
        )
        with self.assertRaises(Exception):
            DelegationRequest(**base, callback_url="http://192.168.0.1/cb")
        with self.assertRaises(Exception):
            DelegationRequest(**base, callback_url="http://localhost:9000/cb")
        with self.assertRaises(Exception):
            DelegationRequest(**base, callback_url="http://0x7f000001/cb")

    def test_transaction_create_callback_url_blocks_private(self):
        # TransactionCreate previously had NO callback validation at all.
        from schemas import TransactionCreate
        with self.assertRaises(Exception):
            TransactionCreate(
                target_agent_id="a1", amount=1, task_description="t",
                callback_url="http://127.0.0.1:8080/cb",
            )
        with self.assertRaises(Exception):
            TransactionCreate(
                target_agent_id="a1", amount=1, task_description="t",
                callback_url="http://169.254.169.254/cb",
            )

    def test_transaction_create_accepts_public_callback(self):
        from schemas import TransactionCreate
        with mock.patch(
            "services.url_guard.socket.getaddrinfo",
            _fake_resolver({"cb.example.com": "93.184.216.34"}),
        ):
            tx = TransactionCreate(
                target_agent_id="a1", amount=1, task_description="t",
                callback_url="https://cb.example.com/cb",
            )
        self.assertEqual(tx.callback_url, "https://cb.example.com/cb")


class TestMcpUrlValidation(unittest.TestCase):
    """MCP registry URLs must reject private destinations with HTTP 400."""

    def test_mcp_validate_url_blocks_metadata(self):
        from fastapi import HTTPException
        from routers.mcp import _validate_url
        with self.assertRaises(HTTPException) as ctx:
            _validate_url("http://169.254.169.254/")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_mcp_validate_url_blocks_loopback(self):
        from fastapi import HTTPException
        from routers.mcp import _validate_url
        with self.assertRaises(HTTPException):
            _validate_url("http://127.0.0.1:9000/sse")

    def test_mcp_validate_url_keeps_scheme_check(self):
        from fastapi import HTTPException
        from routers.mcp import _validate_url
        with self.assertRaises(HTTPException) as ctx:
            _validate_url("ftp://example.com/")
        self.assertEqual(ctx.exception.status_code, 400)


class TestDelegationEndpointGuard(unittest.TestCase):
    """Delegation dispatch must refuse non-public stored endpoints."""

    def test_internal_endpoint_for_prefers_container_dns(self):
        from routers.delegation import _internal_endpoint_for
        self.assertEqual(
            _internal_endpoint_for("abcdef1234567890", "http://ignored/"),
            "http://openclaw-abcdef12:9000",
        )

    def test_validated_target_endpoint_rejects_private(self):
        from routers.delegation import _validated_target_endpoint
        self.assertIsNone(_validated_target_endpoint("http://127.0.0.1:9000/delegate"))
        self.assertIsNone(_validated_target_endpoint("http://169.254.169.254/"))
        self.assertIsNone(_validated_target_endpoint(None))

    def test_validated_target_endpoint_accepts_relative(self):
        # Managed agents store relative paths served by Hive itself.
        from routers.delegation import _validated_target_endpoint
        self.assertEqual(
            _validated_target_endpoint("/agents/x/invoke"),
            "/agents/x/invoke",
        )


class TestSkillDiscoveryGuard(unittest.TestCase):
    """Skill discovery must not probe non-public agent endpoints."""

    def test_discovery_skips_private_endpoint(self):
        import asyncio
        from services.skill_discovery import discover_agent_skills

        class _FakeAgent:  # minimal duck-typed Agent
            endpoint_url = "http://169.254.169.254/latest/meta-data/"

        result = asyncio.get_event_loop().run_until_complete(
            discover_agent_skills(_FakeAgent(), db=None)
        )
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
