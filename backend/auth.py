"""Authentication utilities."""
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import JWTError, jwt
import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from models.user import User
from services import kvstore

# Security configuration
_DEV_MODE = os.getenv("DEV_MODE", "").lower() in ("1", "true", "yes")
_INSECURE_DEFAULT_KEY = "your-secret-key-change-in-production"
SECRET_KEY = os.getenv("SECRET_KEY", "")

if not SECRET_KEY or SECRET_KEY == _INSECURE_DEFAULT_KEY:
    if _DEV_MODE:
        import warnings
        SECRET_KEY = _INSECURE_DEFAULT_KEY
        warnings.warn(
            "Using insecure default SECRET_KEY (DEV_MODE). "
            "Never use this in production!",
            stacklevel=2,
        )
    else:
        raise RuntimeError(
            "SECRET_KEY environment variable is not set. "
            "Set a strong random key, or set DEV_MODE=1 for local development."
        )

ALGORITHM = "HS256"
JWT_ISSUER = "hive-marketplace"
JWT_AUDIENCE = "hive-api"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "15"))  # 15 min short-lived
REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "30"))
REFRESH_COOKIE_NAME = "hive_refresh"
# In production set COOKIE_SECURE=1; in DEV it's False so http://localhost works
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "0" if _DEV_MODE else "1") not in ("0", "false", "no")

security = HTTPBearer()


# ── JWT denylist (revocation) ───────────────────────────────────────────────
# Revoked tokens are recorded by jti with a TTL matching the token's remaining
# lifetime, so the denylist self-prunes and never grows unbounded. Backed by
# Redis in prod (shared across instances) and in-memory in dev.
_DENYLIST_PREFIX = "jwt:revoked:"


async def revoke_token(payload: dict) -> None:
    """Add a token's jti to the denylist for the remainder of its lifetime."""
    jti = payload.get("jti")
    exp = payload.get("exp")
    if not jti or not exp:
        return
    ttl = int(exp - datetime.now(timezone.utc).timestamp())
    if ttl <= 0:
        return
    await kvstore.setex(f"{_DENYLIST_PREFIX}{jti}", "1", ttl)


async def _is_revoked(jti: Optional[str]) -> bool:
    if not jti:
        return False
    return await kvstore.exists(f"{_DENYLIST_PREFIX}{jti}")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash using bcrypt."""
    password_bytes = plain_password.encode('utf-8')
    hash_bytes = hashed_password.encode('utf-8')
    return bcrypt.checkpw(password_bytes, hash_bytes)


def get_password_hash(password: str) -> str:
    """Hash a password using bcrypt."""
    password_bytes = password.encode('utf-8')
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password_bytes, salt)
    return hashed.decode('utf-8')


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create a short-lived JWT access token."""
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    expire = now + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({
        "exp": expire, "iss": JWT_ISSUER, "aud": JWT_AUDIENCE,
        "type": "access", "jti": uuid.uuid4().hex,
    })
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token(user_id: str) -> str:
    """Create a long-lived JWT refresh token stored in an httpOnly cookie."""
    now = datetime.now(timezone.utc)
    expire = now + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    payload = {
        "sub": user_id,
        "exp": expire,
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "type": "refresh",
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def _decode_token(token: str) -> dict:
    """Decode + validate a JWT, raising credentials_exception on failure."""
    try:
        payload = jwt.decode(
            token, SECRET_KEY, algorithms=[ALGORITHM],
            options={"require": ["exp", "iss", "aud", "sub", "jti"]},
            issuer=JWT_ISSUER, audience=JWT_AUDIENCE,
        )
        return payload
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def decode_refresh_token(token: str) -> str:
    """Validate a refresh token and return the user_id (sub claim).

    Checks the denylist so that a rotated/stolen refresh token is rejected on
    reuse (refresh-token reuse detection).
    """
    payload = _decode_token(token)
    if payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )
    if await _is_revoked(payload.get("jti")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has been revoked",
        )
    return payload["sub"]


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db)
) -> User:
    """Get current user from JWT token."""
    payload = _decode_token(credentials.credentials)
    if await _is_revoked(payload.get("jti")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user_id: str = payload.get("sub")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


async def get_current_active_user(
    current_user: User = Depends(get_current_user)
) -> User:
    """Get current active user."""
    if not current_user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")
    return current_user


async def get_user_from_query_token(
    token: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    Authenticate via ?token= query parameter.
    Used for SSE endpoints where EventSource cannot set headers.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
    )
    if not token:
        raise credentials_exception
    payload = _decode_token(token)
    if await _is_revoked(payload.get("jti")):
        raise credentials_exception
    user_id: str = payload.get("sub")
    if user_id is None:
        raise credentials_exception

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise credentials_exception
    return user


async def get_current_admin_user(
    current_user: User = Depends(get_current_active_user)
) -> User:
    """Get current admin user."""
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required"
        )
    return current_user
