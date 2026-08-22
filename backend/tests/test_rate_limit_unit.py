"""Issue #10 — rate-limit fixed-window math without Redis.

Two layers are covered, both on the in-memory (no-Redis) path:

1. ``services.kvstore.fixed_window_count`` — the atomic counter primitive used
   by the distributed limiter; asserts the exact fixed-window semantics
   (limit respected, window resets after expiry, per-key isolation).
2. ``middleware.rate_limit`` — the slowapi limiter configured with
   ``memory://`` storage when ``REDIS_URL`` is unset, plus the login limit
   configuration (Issue #12 default of 5/min) and the 429 handler shape.
"""
import os
import sys
import time
import unittest
import asyncio
from unittest import mock

_BACKEND = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _BACKEND)

# config/auth import cleanly without a real SECRET_KEY only in dev mode.
os.environ.setdefault("SECRET_KEY", "test-secret-key-rate-limit-tests")
os.environ.pop("REDIS_URL", None)  # force the in-memory fallback path

from services import kvstore  # noqa: E402
from middleware import rate_limit as rl  # noqa: E402


def _run(coro):
    """Run a coroutine on the *global* event loop.

    ``asyncio.run()`` closes the loop and clears the current-event-loop
    reference, which breaks later tests that rely on
    ``asyncio.get_event_loop()`` (e.g. test_ssrf_url_guard.py; see the NOTE
    in test_alembic_baseline.py). Reusing one process-global loop keeps the
    whole suite green.
    """
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


def _purge() -> None:
    kvstore._mem_store.clear()
    kvstore._mem_counters.clear()


class TestKvstoreFixedWindow(unittest.TestCase):
    """fixed_window_count math on the in-memory fallback (no Redis)."""

    def setUp(self):
        _purge()

    def tearDown(self):
        _purge()

    def test_allows_up_to_limit_then_blocks(self):
        key = "rl:unit:1"
        seen = []
        for _ in range(8):
            count, allowed = _run(kvstore.fixed_window_count(key, 60, 5))
            seen.append(allowed)
        self.assertEqual(seen, [True] * 5 + [False] * 3)
        # The count keeps incrementing even past the limit (no early exit).
        count, allowed = _run(kvstore.fixed_window_count(key, 60, 5))
        self.assertEqual(count, 9)
        self.assertFalse(allowed)

    def test_window_expiry_allows_again(self):
        """A fresh window resets the counter (fixed-window semantics)."""
        key = "rl:unit:expiry"
        # Burn the whole window at a frozen point in time.
        with mock.patch("services.kvstore.time.time", return_value=1000.0):
            for _ in range(5):
                count, allowed = _run(kvstore.fixed_window_count(key, 10, 5))
            self.assertTrue(allowed)
            count, allowed = _run(kvstore.fixed_window_count(key, 10, 5))
            self.assertFalse(allowed)
        # Well past the window: counter must have reset.
        with mock.patch("services.kvstore.time.time", return_value=1100.0):
            count, allowed = _run(kvstore.fixed_window_count(key, 10, 5))
        self.assertEqual(count, 1)
        self.assertTrue(allowed)

    def test_keys_are_isolated(self):
        for _ in range(5):
            _run(kvstore.fixed_window_count("rl:unit:a", 60, 5))
        count, allowed = _run(kvstore.fixed_window_count("rl:unit:b", 60, 5))
        self.assertEqual((count, allowed), (1, True), "exhausting key A must not throttle key B")

    def test_limit_of_one(self):
        key = "rl:unit:single"
        self.assertEqual(_run(kvstore.fixed_window_count(key, 60, 1)), (1, True))
        self.assertEqual(_run(kvstore.fixed_window_count(key, 60, 1)), (2, False))

    def test_zero_limit_blocks_first_request(self):
        key = "rl:unit:zero"
        self.assertEqual(_run(kvstore.fixed_window_count(key, 60, 0)), (1, False))

    def test_counter_ttl_prunes_state(self):
        """Entries expire from the in-memory store (self-pruning, no leak)."""
        key = "rl:unit:ttl"
        _run(kvstore.fixed_window_count(key, 1, 5))
        with mock.patch("services.kvstore.time.time", return_value=time.time() + 3600):
            count, _ = _run(kvstore.fixed_window_count(key, 1, 5))
        self.assertEqual(count, 1, "stale window entries must be pruned")


class TestRateLimiterConfig(unittest.TestCase):
    """The middleware limiter must run on memory:// when Redis is absent."""

    def test_memory_storage_when_no_redis(self):
        from limits.storage.memory import MemoryStorage
        self.assertIsInstance(rl.limiter._storage, MemoryStorage)
        self.assertEqual(rl._storage_uri, "memory://")

    def test_default_limit_is_200_per_minute(self):
        # LimitGroup keeps the raw "200/minute" string; parse it to compare.
        from limits import parse_many
        groups = [parse_many(g._LimitGroup__limit_provider) for g in rl.limiter._default_limits]
        self.assertEqual([str(p[0]) for p in groups], ["200 per 1 minute"])

    def test_strategy_is_fixed_window(self):
        self.assertEqual(rl.limiter._strategy, "fixed-window")

    def test_login_limit_default_5_per_minute(self):
        self.assertEqual(rl._login_rate_limit(), "5/minute")

    def test_login_limit_configurable(self):
        with mock.patch.dict(os.environ, {"LOGIN_RATE_LIMIT_PER_MIN": "12"}):
            self.assertEqual(rl._login_rate_limit(), "12/minute")

    def test_login_limit_floor_of_one(self):
        with mock.patch.dict(os.environ, {"LOGIN_RATE_LIMIT_PER_MIN": "0"}):
            self.assertEqual(rl._login_rate_limit(), "1/minute")
        with mock.patch.dict(os.environ, {"LOGIN_RATE_LIMIT_PER_MIN": "-5"}):
            self.assertEqual(rl._login_rate_limit(), "1/minute")

    def test_login_limit_survives_garbage(self):
        with mock.patch.dict(os.environ, {"LOGIN_RATE_LIMIT_PER_MIN": "banana"}):
            self.assertEqual(rl._login_rate_limit(), "5/minute")

    def test_lockout_threshold_floor_of_three(self):
        with mock.patch.dict(os.environ, {"LOGIN_LOCKOUT_THRESHOLD": "1"}):
            self.assertEqual(rl._login_lockout_threshold(), 3)


class TestSlowapiFixedWindowMath(unittest.TestCase):
    """slowapi's fixed-window strategy on memory:// — the exact engine the
    middleware uses in dev/no-Redis deployments."""

    def setUp(self):
        from limits.storage.memory import MemoryStorage
        from limits.strategies import FixedWindowRateLimiter
        from limits import parse
        self.storage = MemoryStorage()
        self.strategy = FixedWindowRateLimiter(self.storage)
        self.limit = parse("3/minute")
        self.ident = f"test-{time.time_ns()}"

    def test_blocks_at_limit_and_resets_next_window(self):
        for _ in range(3):
            self.assertTrue(self.strategy.hit(self.limit, self.ident))
        self.assertFalse(self.strategy.hit(self.limit, self.ident),
                         "4th hit inside the window must be blocked")
        stats = self.strategy.get_window_stats(self.limit, self.ident)
        self.assertEqual(stats.remaining, 0)
        # Next window: allowed again (memory storage reads wall-clock via
        # limits.storage.memory.time.time).
        with mock.patch("limits.storage.memory.time.time",
                        return_value=time.time() + 120):
            self.assertTrue(self.strategy.hit(self.limit, self.ident),
                            "a fresh window must reset the counter")

    def test_keys_isolated_per_identity(self):
        self.assertTrue(self.strategy.hit(self.limit, "alice"))
        self.assertTrue(self.strategy.hit(self.limit, "bob"))
        stats = self.strategy.get_window_stats(self.limit, "alice")
        self.assertEqual(stats.remaining, 2)


class TestRateLimitExceededHandler(unittest.TestCase):
    def test_handler_returns_429_json(self):
        from fastapi import Request
        from slowapi.errors import RateLimitExceeded
        from slowapi.wrappers import Limit
        from limits import parse_many

        limit = Limit(
            limit=parse_many("5/minute")[0],
            key_func=lambda r: "x", per_method=False, methods=[], cost=1,
            scope="test", error_message=None, exempt_when=None,
            override_defaults=False,
        )
        exc = RateLimitExceeded(limit)
        request = mock.Mock(spec=Request)
        response = rl.rate_limit_exceeded_handler(request, exc)
        self.assertEqual(response.status_code, 429)
        import json
        body = json.loads(response.body)
        self.assertEqual(body["error"], "rate_limit_exceeded")
        self.assertIn("5 per 1 minute", body["message"])
        self.assertIn("retry_after", body)


if __name__ == "__main__":
    unittest.main()
