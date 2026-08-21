"""Rate limiting middleware for API protection."""
import os
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from fastapi import Request, Response
from fastapi.responses import JSONResponse

from config import REDIS_URL

# Use Redis for distributed rate limiting when configured; fall back to
# process-local memory in dev. slowapi supports redis:// storage URIs directly.
_storage_uri = REDIS_URL if REDIS_URL else "memory://"

# Create limiter instance
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["200/minute"],
    storage_uri=_storage_uri,
    strategy="fixed-window"
)


def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> Response:
    """Custom handler for rate limit exceeded errors."""
    return JSONResponse(
        status_code=429,
        content={
            "error": "rate_limit_exceeded",
            "message": f"Rate limit exceeded: {exc.detail}",
            "retry_after": exc.detail
        }
    )


# Rate limit configurations for different endpoints
def _login_rate_limit() -> str:
    """Per-IP + per-username login limit. Brute-force protection default:
    5 attempts/minute (configurable via LOGIN_RATE_LIMIT_PER_MIN)."""
    try:
        per_min = int(os.environ.get("LOGIN_RATE_LIMIT_PER_MIN", "5"))
        return f"{max(1, per_min)}/minute"
    except ValueError:
        return "5/minute"


def _login_lockout_threshold() -> int:
    """Failed attempts within the lockout window before backoff engages."""
    try:
        return max(3, int(os.environ.get("LOGIN_LOCKOUT_THRESHOLD", "10")))
    except ValueError:
        return 10


RATE_LIMITS = {
    # Authentication endpoints — brute-force protection (per-IP; the login
    # handler additionally tracks per-username failures for lockout backoff)
    "auth_login": _login_rate_limit(),
    "auth_register": "600/hour",
    
    # Agent registration
    "agent_register": "50/hour",
    "agent_invite": "50/hour",
    
    # Delegation (most critical to rate limit)
    "delegate_request": "60/minute",
    "delegate_complete": "60/minute",
    "delegate_callback": "60/minute",
    
    # Marketplace browsing (lenient)
    "marketplace_list": "100/minute",
    "marketplace_detail": "60/minute",
    
    # Wallet operations
    "wallet_balance": "60/minute",
    "wallet_transactions": "30/minute",
    
    # Reviews
    "review_create": "30/hour",
    "review_list": "60/minute",
}
