"""Shared pytest scaffolding for the Hive backend test-suite (Issue #10).

Provides:
  * deterministic boot config (``SECRET_KEY``, ``HIVE_SIGNING_SECRET``,
    ``DEV_MODE``) set BEFORE any backend module is imported, so the suite is
    hermetic: it passes identically in CI (no ``.env``) and locally;
  * a temporary SQLite database (``DATABASE_URL``) so tests never touch
    ``agent_marketplace.db``;
  * an httpx-backed ``TestClient`` fixture bound to the real FastAPI app,
    including lifespan startup (schema creation);
  * helpers to isolate the in-memory kvstore (JWT denylist + rate counters)
    and the slowapi limiter between tests.

NOTE on DEV_MODE: ``auth``/``config`` capture it once at import time, while
``services/url_guard.py`` reads it lazily on every validation call. We import
the app with DEV_MODE=1 (relaxing the boot-time prod-config gate) and then
pop it again so the SSRF guards stay strict for every test.
"""
from __future__ import annotations

import os
import sys
import tempfile

_BACKEND = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# ---- Boot config (must precede any backend import) --------------------------
_TMPDIR = tempfile.mkdtemp(prefix="hive_pytest_")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMPDIR}/test.db"
os.environ.setdefault("SECRET_KEY", "test-secret-key-hive-conftest-not-for-prod")
os.environ.setdefault(
    "HIVE_SIGNING_SECRET", "hive-test-signing-secret-0123456789abcdef"
)
# Force the in-memory Redis fallback regardless of any local .env (dotenv
# never overrides variables that are already present, even when empty).
os.environ["REDIS_URL"] = ""

os.environ["DEV_MODE"] = "1"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import database  # noqa: E402  — reads DATABASE_URL at import time
from services import kvstore  # noqa: E402

from main import app  # noqa: E402  — reads DEV_MODE/SECRET_KEY at import

# Restore the strict default so url_guard's lazy DEV_MODE lookups stay off.
os.environ.pop("DEV_MODE", None)


def purge_kvstore() -> None:
    """Clear the in-memory kvstore fallback (denylist + rate counters)."""
    kvstore._mem_store.clear()
    kvstore._mem_counters.clear()


def reset_rate_limiter() -> None:
    """Reset every slowapi fixed-window counter (memory:// storage)."""
    from middleware.rate_limit import limiter

    try:
        limiter.reset()
    except Exception:  # pragma: no cover - storage-dependent
        pass


@pytest.fixture()
def clean_state():
    """Isolate denylist/rate-counter/limiter state for one test."""
    purge_kvstore()
    reset_rate_limiter()
    yield
    purge_kvstore()
    reset_rate_limiter()


@pytest.fixture(scope="session")
def client():
    """httpx test client bound to the real app (lifespan startup included)."""
    with TestClient(app) as c:  # context = startup (creates the DB schema)
        yield c


@pytest.fixture()
def user_factory(client):
    """Create users via the real /api/auth/register + /api/auth/login."""
    def _make(name: str = "Test User", password: str = "Passw0rd!"):
        import uuid

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

    return _make
