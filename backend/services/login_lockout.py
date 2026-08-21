"""Per-username login lockout backoff (issue #12).

Tracks failed login attempts per username. After LOGIN_LOCKOUT_THRESHOLD
failures inside the window, further attempts are rejected until the window
expires — complementing the per-IP rate limit with a credential-stuffing
defense that survives IP rotation.
"""
import os
import threading
import time

_lock = threading.Lock()
_failures: dict = {}  # username -> [window_start, failure_count]


def _threshold() -> int:
    try:
        return max(3, int(os.environ.get("LOGIN_LOCKOUT_THRESHOLD", "10")))
    except ValueError:
        return 10


def _window_seconds() -> int:
    try:
        return max(60, int(os.environ.get("LOGIN_LOCKOUT_WINDOW_SECONDS", "300")))
    except ValueError:
        return 300


def is_locked_out(username: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    with _lock:
        entry = _failures.get(username)
        if not entry:
            return False
        window_start, count = entry
        if now - window_start >= _window_seconds():
            del _failures[username]
            return False
        return count >= _threshold()


def record_failure(username: str, now: float | None = None) -> None:
    now = time.time() if now is None else now
    with _lock:
        entry = _failures.get(username)
        if not entry or now - entry[0] >= _window_seconds():
            _failures[username] = [now, 1]
        else:
            entry[1] += 1


def clear(username: str) -> None:
    with _lock:
        _failures.pop(username, None)


def reset_all() -> None:
    """Test helper."""
    with _lock:
        _failures.clear()


def assert_not_locked_out(username: str) -> None:
    if is_locked_out(username):
        raise TimeoutError(
            "account temporarily locked due to repeated failed logins; "
            f"try again in {_window_seconds()} seconds"
        )
