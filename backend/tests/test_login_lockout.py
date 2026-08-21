"""Issue #12: per-username login lockout backoff math."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."
))
from services import login_lockout  # noqa: E402


class TestLoginLockout(unittest.TestCase):
    def setUp(self):
        login_lockout.reset_all()
        os.environ.pop("LOGIN_LOCKOUT_THRESHOLD", None)

    def tearDown(self):
        login_lockout.reset_all()

    def test_not_locked_below_threshold(self):
        for _ in range(login_lockout._threshold() - 1):
            login_lockout.record_failure("user@test.com")
        self.assertFalse(login_lockout.is_locked_out("user@test.com"))

    def test_locked_at_threshold(self):
        for _ in range(login_lockout._threshold()):
            login_lockout.record_failure("user@test.com")
        self.assertTrue(login_lockout.is_locked_out("user@test.com"))

    def test_window_expiry_resets(self):
        for _ in range(login_lockout._threshold()):
            login_lockout.record_failure("user@test.com", now=1000.0)
        # Long after the window, lockout expires.
        self.assertFalse(login_lockout.is_locked_out("user@test.com", now=1000.0 + 3600))

    def test_success_clears_failures(self):
        for _ in range(login_lockout._threshold() - 1):
            login_lockout.record_failure("user@test.com")
        login_lockout.clear("user@test.com")
        self.assertFalse(login_lockout.is_locked_out("user@test.com"))

    def test_default_threshold_is_at_least_3(self):
        self.assertGreaterEqual(login_lockout._threshold(), 3)


if __name__ == "__main__":
    unittest.main()
