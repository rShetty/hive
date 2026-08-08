"""Centralised configuration + production safety checks.

Importing this module validates that the mandatory secrets/env vars are present
when not running in DEV_MODE, so misconfigured deployments fail fast at import
time rather than silently degrading security (e.g. falling back to the
``change-me-in-production`` signing secret).
"""
import os
import warnings

_DEV_MODE = os.getenv("DEV_MODE", "").lower() in ("1", "true", "yes")

# ---- Secrets ---------------------------------------------------------------

#: HMAC secret for legacy delegation payload signing/verification.
#: Superseded by per-agent Ed25519 keys but still required during the
#: dual-signing transition window so existing agents keep working.
HIVE_SIGNING_SECRET = os.getenv("HIVE_SIGNING_SECRET", "change-me-in-production")

#: Redis URL for shared state (JWT denylist, rate limits, replay nonces).
#: In DEV_MODE an in-memory fallback is used when this is unset.
REDIS_URL = os.getenv("REDIS_URL", "")

_INSECURE_SIGNING_DEFAULT = "change-me-in-production"


def _fail(message: str) -> None:
    raise RuntimeError(message)


def _warn(message: str) -> None:
    warnings.warn(message, stacklevel=2)


def enforce_prod_config() -> None:
    """Validate security-sensitive env vars when not in DEV_MODE.

    Called once at app startup. In dev it only warns; in prod it raises
    ``RuntimeError`` so the process exits instead of booting insecurely.
    """
    if _DEV_MODE:
        if HIVE_SIGNING_SECRET == _INSECURE_SIGNING_DEFAULT:
            _warn(
                "Using insecure default HIVE_SIGNING_SECRET (DEV_MODE). "
                "Never use this in production!",
            )
        if not REDIS_URL:
            _warn(
                "REDIS_URL unset in DEV_MODE — using in-memory fallback. "
                "State (denylist, rate limits, nonces) will not be shared "
                "across instances or survive restarts.",
            )
        return

    # ---- Production: hard failures ----
    if HIVE_SIGNING_SECRET == _INSECURE_SIGNING_DEFAULT or not HIVE_SIGNING_SECRET:
        _fail(
            "HIVE_SIGNING_SECRET must be set to a strong random value in "
            "production. Generate one with: openssl rand -hex 32"
        )
    if not REDIS_URL:
        _fail(
            "REDIS_URL must be set in production (shared state for JWT "
            "denylist, rate limits, replay-nonce store). Set DEV_MODE=1 "
            "only for local development."
        )


def is_dev_mode() -> bool:
    return _DEV_MODE
