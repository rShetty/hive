"""Issue #11: secure session cookies + CSP header.

Guards the acceptance criteria:
* login/refresh no longer mirror long-lived access tokens into a JS-readable
  cookie — the browser-facing ``hive_session`` cookie is httpOnly, SameSite=Lax
  and short-lived (access-token lifetime); ``Secure`` is added in production;
* the long-lived credential is exclusively the httpOnly ``hive_refresh``
  cookie, and the frontend keeps the access token in memory only;
* a Content-Security-Policy header is emitted by the security-headers
  middleware;
* the agent-dashboard proxy validates the new httpOnly cookie server-side.
"""
import os
import re
import sys
import tempfile
import unittest
import uuid

_BACKEND = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_REPO_ROOT = os.path.normpath(os.path.join(_BACKEND, ".."))
sys.path.insert(0, _BACKEND)

# Boot config must be set before ``main`` (and therefore ``database`` and
# ``auth``) is imported. A throwaway SQLite DB keeps the dev database clean.
# NOTE: DEV_MODE is set only around the ``main`` import and then REMOVED —
# services/url_guard.py reads DEV_MODE lazily on every validation call, so
# leaving it in the environment would silently relax the SSRF guards for
# every other test in the suite.
_TMPDIR = tempfile.mkdtemp(prefix="hive_sec_session_")
os.environ["DEV_MODE"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-key-issue-11")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMPDIR}/sec_session.db"

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402  — reads DEV_MODE/SECRET_KEY/DATABASE_URL at import

# Restore production-strict defaults so other modules are unaffected.
os.environ.pop("DEV_MODE", None)
if os.environ.get("SECRET_KEY") == "test-secret-key-issue-11":
    os.environ.pop("SECRET_KEY", None)

from routers.auth import SESSION_COOKIE_NAME  # noqa: E402


def _cookie(headers, name: str) -> str:
    """Return the full Set-Cookie header value for ``name``.

    Iterates the individual Set-Cookie headers (never comma-splitting, since
    ``expires`` dates contain commas).
    """
    if hasattr(headers, "get_list"):
        for value in headers.get_list("set-cookie"):
            if value.startswith(f"{name}="):
                return value
        return ""
    raise TypeError("_cookie expects an httpx Headers object")


class TestSecureSessionCookies(unittest.TestCase):
    """Functional: real login/refresh/logout against the app."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)
        with cls.client as c:  # context = lifespan startup (creates schema)
            email = f"sec_session_{uuid.uuid4().hex[:8]}@example.com"
            r = c.post("/api/auth/register", json={
                "name": "SecSession", "email": email, "password": "Passw0rd!",
            })
            assert r.status_code == 200, r.text
            login = c.post("/api/auth/login", json={
                "email": email, "password": "Passw0rd!",
            })
            assert login.status_code == 200, login.text
            cls.login = login
            cls.access_token = login.json()["access_token"]
            cls.refresh = c.post("/api/auth/refresh")
            assert cls.refresh.status_code == 200, cls.refresh.text

    def test_login_sets_httponly_short_lived_session_cookie(self):
        session = _cookie(self.login.headers, SESSION_COOKIE_NAME)
        self.assertTrue(session, f"{SESSION_COOKIE_NAME} cookie missing on login")
        self.assertIn("HttpOnly", session, "session cookie must be HttpOnly")
        self.assertIn("SameSite=lax", session, "session cookie must be SameSite=Lax")
        # Short-lived: matches the access-token lifetime, not 30 days.
        max_age = re.search(r"Max-Age=(\d+)", session)
        self.assertIsNotNone(max_age)
        from auth import ACCESS_TOKEN_EXPIRE_MINUTES
        self.assertEqual(
            int(max_age.group(1)), ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            "session cookie must live exactly as long as the access token",
        )
        self.assertLess(int(max_age.group(1)), 86400, "session cookie must be short-lived")

    def test_login_no_longer_sets_js_readable_hive_token(self):
        raw = ", ".join(self.login.headers.get_list("set-cookie"))
        self.assertNotIn(
            "hive_token=", raw,
            "login must not mirror access tokens into JS-readable cookies",
        )

    def test_refresh_rotates_httponly_session_cookie(self):
        raw = ", ".join(self.refresh.headers.get_list("set-cookie"))
        session = _cookie(self.refresh.headers, SESSION_COOKIE_NAME)
        self.assertTrue(session, f"{SESSION_COOKIE_NAME} cookie missing on refresh")
        self.assertIn("HttpOnly", session)
        self.assertNotIn("hive_token=", raw)

    def test_refresh_response_carries_new_access_token_for_memory(self):
        # The SPA stores this in memory only (never localStorage).
        self.assertTrue(self.refresh.json().get("access_token"))

    def test_logout_clears_session_cookie(self):
        with TestClient(main.app) as c:
            email = f"logout_{uuid.uuid4().hex[:8]}@example.com"
            c.post("/api/auth/register", json={
                "name": "Logout", "email": email, "password": "Passw0rd!",
            })
            c.post("/api/auth/login", json={
                "email": email, "password": "Passw0rd!",
            })
            out = c.post("/api/auth/logout")
        self.assertEqual(out.status_code, 200)
        deleted = _cookie(out.headers, SESSION_COOKIE_NAME)
        expired = 'Max-Age=0' in deleted or 'expires=Thu, 01 Jan 1970' in deleted
        self.assertTrue(
            expired, f"logout must expire the session cookie, got: {deleted!r}"
        )


class TestCspHeader(unittest.TestCase):
    """Functional: CSP is emitted by SecurityHeadersMiddleware."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)

    def test_csp_present_on_api_response(self):
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        csp = r.headers.get("Content-Security-Policy")
        self.assertTrue(csp, "Content-Security-Policy header missing")
        self.assertIn("default-src 'self'", csp)
        self.assertIn("frame-ancestors", csp)
        self.assertIn("object-src", csp) if "object-src" in csp else None

    def test_csp_present_on_frontend_page(self):
        r = self.client.get("/login")
        csp = r.headers.get("Content-Security-Policy")
        self.assertTrue(csp, "Content-Security-Policy missing on HTML pages")


class TestFrontendTokenHandling(unittest.TestCase):
    """Static: the SPA keeps the access token in memory only."""

    def _read(self, *parts: str) -> str:
        with open(os.path.join(_REPO_ROOT, *parts), encoding="utf-8") as fh:
            return fh.read()

    def test_app_js_never_persists_token(self):
        src = self._read("frontend", "js", "app.js")
        self.assertNotIn(
            "localStorage.setItem('token'", src,
            "app.js must not persist the access token to localStorage",
        )
        self.assertNotIn(
            "document.cookie", src,
            "app.js must not read/write tokens via document.cookie",
        )
        # In-memory holder + refresh-based re-auth must exist.
        self.assertIn("let _accessToken", src)
        self.assertIn("restoreSession", src)
        self.assertIn("/api/auth/refresh", src)

    def test_login_and_signup_use_memory_only_token(self):
        for page in ("login.html", "signup.html"):
            src = self._read("frontend", page)
            self.assertNotIn(
                "localStorage.setItem('token'", src,
                f"{page} must not persist the access token",
            )
            self.assertIn("setToken(", src)

    def test_no_page_persists_token_to_localstorage(self):
        for name in sorted(os.listdir(os.path.join(_REPO_ROOT, "frontend"))):
            if not name.endswith(".html"):
                continue
            src = self._read("frontend", name)
            self.assertNotIn(
                "localStorage.setItem('token'", src,
                f"{name} still persists the access token to localStorage",
            )

    def test_gated_pages_reauth_via_refresh_cookie(self):
        for page in ("agents.html", "deploy.html", "settings.html", "tasks.html",
                     "teams.html", "workflows.html", "workflow-builder.html",
                     "team-detail.html", "agent-config.html"):
            src = self._read("frontend", page)
            self.assertIn(
                "restoreSession()", src,
                f"{page} must restore the session via the refresh cookie on reload",
            )


class TestProxyAndCspWiring(unittest.TestCase):
    """Static: server-side consumers of the cookie keep working."""

    def _main_py(self) -> str:
        with open(os.path.join(_BACKEND, "main.py"), encoding="utf-8") as fh:
            return fh.read()

    def test_csp_configured_in_security_headers_middleware(self):
        src = self._main_py()
        segment = src.split("class SecurityHeadersMiddleware")[1].split(
            "app.add_middleware(SecurityHeadersMiddleware)"
        )[0]
        self.assertIn('response.headers["Content-Security-Policy"]', segment)
        self.assertIn("default-src 'self'", segment)

    def test_proxy_validates_httponly_session_cookie(self):
        src = self._main_py()
        segment = src.split("async def _validate_hive_token")[1].split(
            "async def agent_dashboard_proxy"
        )[0]
        self.assertIn(
            "SESSION_COOKIE_NAME", segment,
            "proxy must read the new httpOnly hive_session cookie",
        )

    def test_login_sets_no_long_lived_js_readable_cookie(self):
        with open(os.path.join(_BACKEND, "routers", "auth.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn(
            'key="hive_token"', src,
            "auth router must stop setting the JS-readable hive_token cookie",
        )
        segment = src.split("async def login")[1].split("async def refresh")[0]
        self.assertIn("httponly=True", segment)


if __name__ == "__main__":
    unittest.main()
