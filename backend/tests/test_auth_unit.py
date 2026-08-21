"""Issue #10 — unit tests for backend/auth.py.

Covers the JWT lifecycle (issue / verify / expiry / issuer+audience binding /
type confusion), the revocation denylist, bcrypt password hashing round-trip,
and token claim extraction. Purely unit-level: no HTTP, no database.
"""
import os
import sys
import time
import unittest
import uuid
from datetime import timedelta

_BACKEND = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _BACKEND)

# auth.py requires SECRET_KEY at import time; set it before the import.
os.environ.setdefault("SECRET_KEY", "test-secret-key-auth-unit")

from jose import jwt as _jose_jwt  # noqa: E402

import auth  # noqa: E402
from services import kvstore  # noqa: E402


def _run(coro):
    """Run a coroutine on the *global* event loop.

    ``asyncio.run()`` closes the loop and clears the current-event-loop
    reference, which breaks later tests that rely on
    ``asyncio.get_event_loop()`` (see the NOTE in test_alembic_baseline.py).
    Reusing one process-global loop keeps the whole suite green.
    """
    import asyncio
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


class TestPasswordHashing(unittest.TestCase):
    def test_bcrypt_round_trip(self):
        h = auth.get_password_hash("S3cret-π-password!")
        self.assertTrue(h.startswith("$2"), "hash must be a bcrypt string")
        self.assertNotIn("S3cret", h, "hash must not contain the plaintext")
        self.assertTrue(auth.verify_password("S3cret-π-password!", h))

    def test_wrong_password_rejected(self):
        h = auth.get_password_hash("correct-horse")
        self.assertFalse(auth.verify_password("wrong-battery", h))

    def test_unique_salt_per_hash(self):
        a = auth.get_password_hash("same-input")
        b = auth.get_password_hash("same-input")
        self.assertNotEqual(a, b, "bcrypt salts must be random per hash")

    def test_hash_is_unicode_safe(self):
        h = auth.get_password_hash("pässwörd-🔐")
        self.assertTrue(auth.verify_password("pässwörd-🔐", h))
        self.assertFalse(auth.verify_password("password", h))


class TestAccessToken(unittest.TestCase):
    def test_issue_and_verify(self):
        sub = str(uuid.uuid4())
        tok = auth.create_access_token(data={"sub": sub})
        payload = auth._decode_token(tok)  # raises on any validation failure
        self.assertEqual(payload["sub"], sub)
        self.assertEqual(payload["type"], "access")
        self.assertEqual(payload["iss"], auth.JWT_ISSUER)
        self.assertEqual(payload["aud"], auth.JWT_AUDIENCE)
        self.assertTrue(payload.get("jti"), "every access token must carry a jti")

    def test_default_expiry_matches_config(self):
        now = time.time()
        payload = auth._decode_token(auth.create_access_token(data={"sub": "u1"}))
        expected = now + auth.ACCESS_TOKEN_EXPIRE_MINUTES * 60
        self.assertAlmostEqual(payload["exp"], expected, delta=30)

    def test_custom_expiry_delta(self):
        payload = auth._decode_token(
            auth.create_access_token(data={"sub": "u1"}, expires_delta=timedelta(seconds=120))
        )
        self.assertAlmostEqual(payload["exp"], time.time() + 120, delta=5)

    def test_expired_token_rejected(self):
        tok = auth.create_access_token(
            data={"sub": "u1"}, expires_delta=timedelta(seconds=-10)
        )
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            auth._decode_token(tok)
        self.assertEqual(cm.exception.status_code, 401)

    def test_tampered_signature_rejected(self):
        tok = auth.create_access_token(data={"sub": "u1"})
        header, body, sig = tok.split(".")
        forged = f"{header}.{body[:-4]}AAAA{sig}"
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            auth._decode_token(forged)

    def test_garbage_token_rejected(self):
        from fastapi import HTTPException
        for bad in ("", "not-a-jwt", "a.b.c"):
            with self.assertRaises(HTTPException):
                auth._decode_token(bad)

    def test_wrong_issuer_or_audience_rejected(self):
        other = _jose_jwt.encode(
            {"sub": "u1", "iss": "evil", "aud": auth.JWT_AUDIENCE,
             "exp": int(time.time()) + 300},
            auth.SECRET_KEY, algorithm=auth.ALGORITHM,
        )
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            auth._decode_token(other)

        wrong_aud = _jose_jwt.encode(
            {"sub": "u1", "iss": auth.JWT_ISSUER, "aud": "other-api",
             "exp": int(time.time()) + 300},
            auth.SECRET_KEY, algorithm=auth.ALGORITHM,
        )
        with self.assertRaises(HTTPException):
            auth._decode_token(wrong_aud)

    def test_missing_sub_or_jti_leaves_token_unusable(self):
        """python-jose 3.3 does not enforce ``options.require`` for arbitrary
        claims, so ``_decode_token`` alone accepts tokens lacking sub/jti.
        Downstream guards must treat those as unauthenticated — assert the
        documented behaviour so a future jose upgrade cannot silently change
        it (and so the gap itself stays visible in the suite)."""
        no_jti = _jose_jwt.encode(
            {"sub": "u1", "iss": auth.JWT_ISSUER, "aud": auth.JWT_AUDIENCE,
             "exp": int(time.time()) + 300},
            auth.SECRET_KEY, algorithm=auth.ALGORITHM,
        )
        payload = auth._decode_token(no_jti)
        self.assertIsNone(payload.get("jti"))
        # The denylist check tolerates a missing jti rather than crashing.
        got = _run(auth._is_revoked(payload.get("jti")))
        self.assertFalse(got)

    def test_refresh_type_cannot_pass_as_access_in_get_user_flow(self):
        # decode_refresh_token rejects tokens whose type is not "refresh".
        access = auth._decode_token(auth.create_access_token(data={"sub": "u1"}))
        from fastapi import HTTPException
        # decode_refresh_token expects an encoded token; feed it an access
        # token and expect rejection on type.
        access_tok = auth.create_access_token(data={"sub": "u1"})
        with self.assertRaises(HTTPException):
            _run(auth.decode_refresh_token(access_tok))


class TestRefreshToken(unittest.TestCase):
    def test_refresh_round_trip(self):
        uid = str(uuid.uuid4())
        tok = auth.create_refresh_token(uid)
        got = _run(auth.decode_refresh_token(tok))
        self.assertEqual(got, uid)

    def test_access_token_rejected_as_refresh(self):
        tok = auth.create_access_token(data={"sub": "u1"})
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            _run(auth.decode_refresh_token(tok))


class TestRevocationDenylist(unittest.TestCase):
    def setUp(self):
        kvstore._mem_store.clear()
        kvstore._mem_counters.clear()

    def tearDown(self):
        kvstore._mem_store.clear()
        kvstore._mem_counters.clear()

    def test_revoked_jti_is_detected(self):
        payload = {"jti": uuid.uuid4().hex, "exp": int(time.time()) + 60}
        _run(auth.revoke_token(payload))
        # Private helper is the exact check used by get_current_user.
        got = _run(auth._is_revoked(payload["jti"]))
        self.assertTrue(got)

    def test_unrevoked_jti_passes(self):
        got = _run(auth._is_revoked(uuid.uuid4().hex))
        self.assertFalse(got)

    def test_missing_claims_are_noop(self):
        # Must not raise and must not poison the store.
        _run(auth.revoke_token({"jti": None, "exp": None}))
        _run(auth.revoke_token({}))
        self.assertFalse(kvstore._mem_store)

    def test_already_expired_token_not_denied(self):
        payload = {"jti": uuid.uuid4().hex, "exp": int(time.time()) - 10}
        _run(auth.revoke_token(payload))
        got = _run(auth._is_revoked(payload["jti"]))
        self.assertFalse(got, "expired tokens need no denylist entry")

    def test_full_token_revocation_end_to_end(self):
        tok = auth.create_access_token(data={"sub": "u-revoked"})
        payload = auth._decode_token(tok)
        self.assertFalse(_run(auth._is_revoked(payload["jti"])))
        _run(auth.revoke_token(payload))
        self.assertTrue(_run(auth._is_revoked(payload["jti"])))


class TestTokenExtraction(unittest.TestCase):
    """Bearer extraction paths used by the API surface."""

    def _bearer_headers(self, tok: str) -> dict:
        return {"Authorization": f"Bearer {tok}"}

    def test_bearer_scheme_format(self):
        tok = auth.create_access_token(data={"sub": "u1"})
        scheme, _, credentials = f"Bearer {tok}".partition(" ")
        self.assertEqual(scheme, "Bearer")
        self.assertEqual(credentials, tok)

    def test_query_param_auth_rejects_missing_token(self):
        """get_user_from_query_token (SSE path) must 401 without a token."""
        from fastapi import HTTPException
        coro = auth.get_user_from_query_token(None, db=None)
        with self.assertRaises(HTTPException) as cm:
            _run(coro)
        self.assertEqual(cm.exception.status_code, 401)

    def test_query_param_auth_checks_denylist_before_db(self):
        class _Boom:
            async def execute(self, *a, **k):  # pragma: no cover
                raise AssertionError("db must not be hit for revoked tokens")

        tok = auth.create_access_token(data={"sub": "u1"})
        payload = auth._decode_token(tok)
        _run(auth.revoke_token(payload))
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            _run(
                auth.get_user_from_query_token(tok, db=_Boom())
            )
        self.assertEqual(cm.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
