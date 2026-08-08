"""Authentication routes."""
from fastapi import APIRouter, Depends, HTTPException, status, Request, Cookie, Response
from fastapi.security import HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Optional

from database import get_db
from models.user import User
from schemas import UserCreate, UserResponse, Token, LoginRequest
from auth import (
    verify_password, get_password_hash, create_access_token, get_current_active_user,
    create_refresh_token, decode_refresh_token, revoke_token, _decode_token,
    REFRESH_COOKIE_NAME, REFRESH_TOKEN_EXPIRE_DAYS, COOKIE_SECURE,
)
from middleware.rate_limit import limiter, RATE_LIMITS

router = APIRouter(prefix="/api/auth", tags=["auth"])
security = HTTPBearer()
_optional_security = HTTPBearer(auto_error=False)


@router.post("/register", response_model=UserResponse)
@limiter.limit(RATE_LIMITS["auth_register"])
async def register(request: Request, user_data: UserCreate, db: AsyncSession = Depends(get_db)):
    """Register a new user."""
    # Check if email already exists
    result = await db.execute(select(User).where(User.email == user_data.email))
    existing_user = result.scalar_one_or_none()
    
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered"
        )
    
    # Create new user
    hashed_password = get_password_hash(user_data.password)
    new_user = User(
        email=user_data.email,
        hashed_password=hashed_password,
        name=user_data.name
    )
    
    db.add(new_user)
    await db.flush()  # Flush to get user.id
    
    # Create wallet with initial balance
    from models.wallet import Wallet
    from decimal import Decimal
    
    wallet = Wallet(
        user_id=new_user.id,
        balance=Decimal("100.00")  # Initial 100 tokens
    )
    db.add(wallet)
    
    await db.commit()
    await db.refresh(new_user)
    
    print(f"🎉 New user registered: {new_user.email} (Wallet created with 100 tokens)")
    
    return new_user


@router.post("/login", response_model=Token)
@limiter.limit(RATE_LIMITS["auth_login"])
async def login(request: Request, response: Response, login_data: LoginRequest, db: AsyncSession = Depends(get_db)):
    """Login — returns a short-lived access token and sets a long-lived refresh cookie."""
    result = await db.execute(select(User).where(User.email == login_data.email))
    user = result.scalar_one_or_none()

    if not user or not verify_password(login_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token = create_access_token(data={"sub": user.id})
    refresh_token = create_refresh_token(user.id)

    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=refresh_token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        path="/api/auth",  # cookie only sent to auth endpoints
    )

    # Non-httpOnly session cookie used by the agent dashboard proxy (/a/{slug}/).
    # It carries the same access token that lives in localStorage — no additional
    # secret is exposed. Path is "/" so the proxy middleware can read it.
    response.set_cookie(
        key="hive_token",
        value=access_token,
        httponly=False,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=REFRESH_TOKEN_EXPIRE_DAYS * 86400,  # long-lived; proxy refreshes token when needed
        path="/",
    )

    return {"access_token": access_token, "token_type": "bearer"}


@router.post("/refresh", response_model=Token)
async def refresh_access_token(
    response: Response,
    db: AsyncSession = Depends(get_db),
    hive_refresh: Optional[str] = Cookie(default=None, alias=REFRESH_COOKIE_NAME),
):
    """
    Issue a new access token using the httpOnly refresh cookie.
    Also rotates the refresh token (issues a new cookie) for sliding expiry.
    """
    if not hive_refresh:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No refresh token")

    # Decode the old refresh token to capture its jti for revocation (rotation).
    # decode_refresh_token is async and also enforces the denylist, so a reused
    # / stolen refresh token is rejected here.
    old_payload = _decode_token(hive_refresh)
    user_id = await decode_refresh_token(hive_refresh)  # raises 401 if invalid/revoked

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")

    # Rotate: revoke the old refresh token and issue fresh tokens (sliding window).
    await revoke_token(old_payload)
    new_access_token = create_access_token(data={"sub": user.id})
    new_refresh_token = create_refresh_token(user.id)

    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=new_refresh_token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        path="/api/auth",
    )
    response.set_cookie(
        key="hive_token",
        value=new_access_token,
        httponly=False,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        path="/",
    )

    return {"access_token": new_access_token, "token_type": "bearer"}


@router.post("/logout")
async def logout(
    response: Response,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_optional_security),
    hive_refresh: Optional[str] = Cookie(default=None, alias=REFRESH_COOKIE_NAME),
):
    """Revoke the refresh + access tokens and clear auth cookies.

    Both tokens are added to the denylist for the remainder of their lifetimes,
    so a stolen token can't be reused after the user logs out.
    """
    # Revoke refresh token if present.
    if hive_refresh:
        try:
            await revoke_token(_decode_token(hive_refresh))
        except HTTPException:
            pass  # malformed/expired — nothing to revoke

    # Revoke access token from the Authorization header if present.
    if credentials and credentials.credentials:
        try:
            await revoke_token(_decode_token(credentials.credentials))
        except HTTPException:
            pass

    response.delete_cookie(key=REFRESH_COOKIE_NAME, path="/api/auth")
    response.delete_cookie(key="hive_token", path="/")
    return {"message": "Logged out"}


@router.get("/me", response_model=UserResponse)
async def get_current_user_profile(
    current_user: User = Depends(get_current_active_user),
):
    """Get the currently authenticated user's profile."""
    return current_user
