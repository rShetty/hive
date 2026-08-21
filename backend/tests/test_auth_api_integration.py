"""Issue #10 — integration tests: pytest + httpx test client against the real
app backed by a temporary SQLite database (via conftest.py).

Exercises the auth surface end-to-end over HTTP:
  * register → login → /api/auth/me round trip;
  * wrong password rejected with 401;
  * protected endpoints reject missing/garbage/expired Bearer tokens;
  * logout revokes the access token (denylist enforced on the next call);
  * rate-limit middleware responds 429 when a limit is exhausted
    (in-memory storage, no Redis).
"""
import os
import sys
import time
import unittest
import uuid

_BACKEND = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _BACKEND)

# Boot config for a hermetic run (no .env needed). conftest.py sets the full
# set too, but this module must also be runnable via `python test_auth_api_integration.py`.
os.environ.setdefault("SECRET_KEY", "test-secret-key-auth-api")
os.environ.setdefault(
    "HIVE_SIGNING_SECRET", "hive-test-signing-secret-0123456789abcdef"
)
os.environ["REDIS_URL"] = ""
os.environ.pop("DEV_MODE", None)  # keep url_guard strict

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402  — reads DEV_MODE/SECRET_KEY/DATABASE_URL at import
from services import kvstore  # noqa: E402


def _purge() -> None:
    kvstore._mem_store.clear()
    kvstore._mem_counters.clear()
    from middleware.rate_limit import limiter
    try:
        limiter.reset()
    except Exception:
        pass


_CLIENT = None  # session TestClient, wired by the pytest fixture below


def _make_user(client, name: str = "Test User", password: str = "Passw0rd!") -> dict:
    email = f"{uuid.uuid4().hex[:10]}@example.com"
    r = client.post("/api/auth/register", json={
        "name": name, "email": email, "password": password,
    })
    assert r.status_code == 200, r.text
    login = client.post("/api/auth/login", json={
        "email": email, "password": password,
    })
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    return {
        "email": email, "password": password, "name": name,
        "token": token,
        "headers": {"Authorization": f"Bearer {token}"},
    }


# ---- pytest adapter so unittest classes share the session-lifespan client ---
import pytest as _pytest  # noqa: E402


@_pytest.fixture(autouse=True)
def _inject_client(client):
    global _CLIENT
    _CLIENT = client
    yield


class TestAuthApiFlow(unittest.TestCase):
    """Runs inside the session TestClient so lifespan has created the schema."""

    @property
    def client(self):
        return _CLIENT

    def test_register_login_me_round_trip(self):
        user = _make_user(self.client, name="Auth API")
        r = self.client.get("/api/auth/me", headers=user["headers"])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["email"], user["email"])

    def test_login_wrong_password_401(self):
        user = _make_user(self.client)
        r = self.client.post("/api/auth/login", json={
            "email": user["email"], "password": "totally-wrong",
        })
        self.assertIn(r.status_code, (401, 429))

    def test_register_duplicate_email_rejected(self):
        user = _make_user(self.client)
        r = self.client.post("/api/auth/register", json={
            "name": "Dup", "email": user["email"], "password": "Other123!",
        })
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["detail"], "Email already registered")

    def test_me_requires_bearer_token(self):
        r = self.client.get("/api/auth/me")
        self.assertIn(r.status_code, (401, 403))

    def test_me_rejects_garbage_token(self):
        r = self.client.get("/api/auth/me", headers={
            "Authorization": "Bearer garbage.token.here",
        })
        self.assertEqual(r.status_code, 401)

    def test_me_rejects_expired_token(self):
        from auth import SECRET_KEY, ALGORITHM, JWT_ISSUER, JWT_AUDIENCE
        from jose import jwt as jose_jwt
        expired = jose_jwt.encode(
            {"sub": "someone", "exp": int(time.time()) - 60,
             "iss": JWT_ISSUER, "aud": JWT_AUDIENCE, "jti": uuid.uuid4().hex},
            SECRET_KEY, algorithm=ALGORITHM,
        )
        r = self.client.get("/api/auth/me", headers={
            "Authorization": f"Bearer {expired}",
        })
        self.assertEqual(r.status_code, 401)


class TestLogoutRevocation(unittest.TestCase):
    def setUp(self):
        _purge()

    def tearDown(self):
        _purge()

    def test_logout_denies_access_token_afterwards(self):
        with TestClient(main.app) as c:

            email = f"logout_rev_{uuid.uuid4().hex[:8]}@example.com"
            c.post("/api/auth/register", json={
                "name": "LogoutRev", "email": email, "password": "Passw0rd!",
            })
            login = c.post("/api/auth/login", json={
                "email": email, "password": "Passw0rd!",
            })
            tok = login.json()["access_token"]
            headers = {"Authorization": f"Bearer {tok}"}
            self.assertEqual(c.get("/api/auth/me", headers=headers).status_code, 200)

            out = c.post("/api/auth/logout", headers=headers)
            self.assertEqual(out.status_code, 200)

            # Same token must now be denied by the revocation denylist.
            r = c.get("/api/auth/me", headers=headers)
            self.assertEqual(
                r.status_code, 401,
                "revoked access token must be rejected after logout",
            )

    def test_refresh_cookie_rotation_and_reuse_detection(self):
        with TestClient(main.app) as c:
            email = f"rot_{uuid.uuid4().hex[:8]}@example.com"
            c.post("/api/auth/register", json={
                "name": "Rot", "email": email, "password": "Passw0rd!",
            })
            first = c.post("/api/auth/login", json={
                "email": email, "password": "Passw0rd!",
            })
            self.assertEqual(first.status_code, 200)

            # Capture the ORIGINAL refresh cookie before rotating.
            original_refresh = c.cookies.get("hive_refresh")
            self.assertTrue(original_refresh)

            # Refresh rotates both tokens (old refresh jti → denylist).
            r1 = c.post("/api/auth/refresh")
            self.assertEqual(r1.status_code, 200)
            self.assertNotEqual(
                c.cookies.get("hive_refresh"), original_refresh,
                "refresh must rotate the hive_refresh cookie",
            )

            # Replaying the ORIGINAL refresh cookie after rotation must
            # fail — decode_refresh_token consults the denylist for the
            # rotated-out jti (refresh-token reuse detection).
            c.cookies.set("hive_refresh", original_refresh)
            reused = c.post("/api/auth/refresh")
            self.assertEqual(
                reused.status_code, 401,
                "a rotated-out refresh token must be denied on reuse",
            )


class TestRateLimitOverHttp(unittest.TestCase):
    """The middleware returns 429 once an endpoint limit is exhausted."""

    @property
    def client(self):
        return _CLIENT

    def setUp(self):
        _purge()

    def tearDown(self):
        _purge()

    def _login_as(self, ip: str, email: str):
        # slowapi's get_remote_address keys on request.client.host; the
        # TestClient always presents 127.0.0.1 (testserver), so per-IP
        # differentiation is not possible here. Isolation comes from
        # limiter.reset() in setUp/tearDown instead.
        del ip  # identity is fixed by the test transport
        return self.client.post(
            "/api/auth/login",
            json={"email": email, "password": "nope"},
        )

    def test_health_endpoint_unthrottled_under_normal_use(self):
        codes = [self.client.get("/api/health").status_code for _ in range(10)]
        self.assertTrue(all(c == 200 for c in codes))

    def test_rate_limit_exceeded_handler_is_wired(self):
        # main registers the handler for RateLimitExceeded, so any exceeded
        # limit yields the JSON 429 shape instead of an unhandled exception.
        from slowapi.errors import RateLimitExceeded
        handler = main.app.exception_handlers.get(RateLimitExceeded)
        self.assertIsNotNone(handler, "429 handler must be registered on app")

    def test_login_limit_enforced_per_ip(self):
        """5/min default: the 6th login attempt inside the window gets 429."""
        email = f"rl_ip_{uuid.uuid4().hex[:8]}@example.com"
        codes = []
        for _ in range(6):
            r = self._login_as("testclient", email)
            codes.append(r.status_code)
        self.assertEqual(codes[:5], [401] * 5, "wrong-password attempts are 401")
        self.assertEqual(
            codes[5], 429,
            f"6th login inside the window must be rate limited, got {codes}",
        )


if __name__ == "__main__":
    unittest.main()
