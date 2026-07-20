"""Rate limiting middleware for API protection."""
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from fastapi import Request, Response
from fastapi.responses import JSONResponse


# Create limiter instance
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["200/minute"],
    storage_uri="memory://",
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
RATE_LIMITS = {
    # Authentication endpoints
    "auth_login": "10/minute",
    "auth_register": "50/hour",
    
    # Agent registration
    "agent_register": "50/hour",
    "agent_invite": "50/hour",
    
    # Delegation (most critical to rate limit)
    "delegate_request": "10/minute",
    "delegate_complete": "20/minute",
    "delegate_callback": "30/minute",
    
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
