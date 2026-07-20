"""OAuth 2.0 "Connect" flow for MCP servers.

Supports the MCP-recommended browser OAuth dance:

  1. GET  /api/mcp-servers/{id}/connect
        -> discovers the provider's authorization-server metadata and
           (if needed) dynamically registers a client, then returns an
           authorization URL using PKCE. The frontend opens it in a popup.
  2. GET  /api/mcp/oauth/callback?code=...&state=...
        -> exchanges the code for tokens, stores them encrypted on the
           MCPServer row (auth_type='oauth'), then redirects to the
           frontend registry page.

State/PKCE verifiers are held in an in-memory cache keyed by state.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from typing import Optional
from urllib.parse import urlencode, urljoin, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.mcp import MCPServer
from models.user import User
from schemas import MCPServerResponse
from services.crypto import encrypt_json, decrypt_json
from auth import get_current_active_user

router = APIRouter(prefix="/api/mcp", tags=["mcp-oauth"])

# In-memory PKCE/state cache (single-instance; good enough for this deploy).
_STATE: dict = {}

CALLBACK_PATH = "/api/mcp/oauth/callback"


def _public_base() -> str:
    return os.getenv("MARKETPLACE_URL", "http://localhost:8080").rstrip("/")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _gen_pkce() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


async def _discover(server: MCPServer) -> dict:
    """Discover OAuth metadata; fall back to conventional endpoints."""
    base = server.url.rstrip("/")
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        meta_url = urljoin(base + "/", ".well-known/oauth-authorization-server")
        try:
            r = await client.get(meta_url)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        origin = f"{urlparse(base).scheme}://{urlparse(base).netloc}"
        try:
            r = await client.get(urljoin(origin + "/", ".well-known/oauth-authorization-server"))
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
    return {
        "authorization_endpoint": urljoin(base + "/", "authorize"),
        "token_endpoint": urljoin(base + "/", "token"),
        "registration_endpoint": urljoin(base + "/", "register"),
    }


async def _register_client(server: MCPServer, meta: dict, redirect_uri: str) -> tuple[str, Optional[str]]:
    """Dynamically register a client if no client_id is stored yet."""
    stored = decrypt_json(server.oauth_encrypted) or {}
    if stored.get("client_id"):
        return stored["client_id"], stored.get("client_secret")

    reg_ep = meta.get("registration_endpoint")
    if not reg_ep:
        return "", None
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        r = await client.post(
            reg_ep,
            json={
                "client_name": "Hive OpenClaw",
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
            headers={"Content-Type": "application/json"},
        )
        if r.status_code >= 400:
            raise HTTPException(502, f"OAuth client registration failed: {r.status_code}")
        data = r.json()
        client_id = data.get("client_id")
        client_secret = data.get("client_secret")
        blob = {**(stored or {}), "client_id": client_id, "client_secret": client_secret}
        server.oauth_encrypted = encrypt_json(blob)
        return client_id, client_secret


@router.get("/servers/{server_id}/connect")
async def connect(
    server_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Begin the OAuth connect flow; return an authorization URL + state."""
    result = await db.execute(select(MCPServer).where(MCPServer.id == server_id))
    server = result.scalar_one_or_none()
    if not server:
        raise HTTPException(404, "MCP server not found")
    if server.owner_id != current_user.id:
        raise HTTPException(403, "Not your MCP server")
    if server.auth_type != "oauth":
        raise HTTPException(400, "This server is not configured for OAuth")

    redirect_uri = _public_base() + CALLBACK_PATH
    meta = await _discover(server)
    auth_ep = meta.get("authorization_endpoint")
    if not auth_ep:
        raise HTTPException(502, "Could not discover OAuth authorization endpoint")
    client_id, _ = await _register_client(server, meta, redirect_uri)
    await db.commit()

    verifier, challenge = _gen_pkce()
    state = secrets.token_urlsafe(24)
    _STATE[state] = {
        "server_id": server_id,
        "user_id": current_user.id,
        "verifier": verifier,
        "token_endpoint": meta.get("token_endpoint"),
        "client_id": client_id,
        "redirect_uri": redirect_uri,
    }

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "scope": (meta.get("scopes_supported") or ["openid"])[0],
    }
    auth_url = auth_ep + "?" + urlencode(params)
    return {"authorize_url": auth_url, "state": state}


@router.get("/oauth/callback")
async def callback(
    code: str = Query(...),
    state: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """OAuth redirect target: exchange code for tokens and store them."""
    flow = _STATE.pop(state, None)
    if not flow:
        raise HTTPException(400, "Invalid or expired OAuth state")
    if not flow.get("token_endpoint"):
        raise HTTPException(502, "Missing token endpoint")

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": flow["redirect_uri"],
        "client_id": flow["client_id"],
        "code_verifier": flow["verifier"],
    }
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        r = await client.post(flow["token_endpoint"], data=data)
        if r.status_code >= 400:
            raise HTTPException(502, f"Token exchange failed: {r.status_code} {r.text[:200]}")
        tokens = r.json()

    result = await db.execute(select(MCPServer).where(MCPServer.id == flow["server_id"]))
    server = result.scalar_one_or_none()
    if not server:
        raise HTTPException(404, "MCP server not found")
    blob = decrypt_json(server.oauth_encrypted) or {}
    blob.update({
        "access_token": tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
        "token_type": tokens.get("token_type", "Bearer"),
        "expires_in": tokens.get("expires_in"),
        "scope": tokens.get("scope"),
        "token_endpoint": flow.get("token_endpoint"),
    })
    server.oauth_encrypted = encrypt_json(blob)
    server.auth_type = "oauth"
    await db.commit()
    return RedirectResponse(url=f"{_public_base()}/mcp?connected={server.id}")


@router.post("/servers/{server_id}/refresh")
async def refresh(
    server_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Refresh the OAuth access token using the stored refresh token."""
    result = await db.execute(select(MCPServer).where(MCPServer.id == server_id))
    server = result.scalar_one_or_none()
    if not server:
        raise HTTPException(404, "MCP server not found")
    if server.owner_id != current_user.id:
        raise HTTPException(403, "Not your MCP server")
    blob = decrypt_json(server.oauth_encrypted) or {}
    refresh_token = blob.get("refresh_token")
    token_endpoint = blob.get("token_endpoint")
    if not refresh_token or not token_endpoint:
        raise HTTPException(400, "No refresh token available")

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        r = await client.post(
            token_endpoint,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": blob.get("client_id"),
            },
        )
        if r.status_code >= 400:
            raise HTTPException(502, f"Refresh failed: {r.status_code}")
        tokens = r.json()
    blob.update({
        "access_token": tokens.get("access_token", blob.get("access_token")),
        "refresh_token": tokens.get("refresh_token", refresh_token),
        "expires_in": tokens.get("expires_in"),
    })
    server.oauth_encrypted = encrypt_json(blob)
    await db.commit()
    return {"ok": True}
