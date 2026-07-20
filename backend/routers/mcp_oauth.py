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
    """Discover OAuth metadata; fall back to conventional endpoints.

    Returns a dict with at least ``authorization_endpoint`` and
    ``token_endpoint``. Providers that do not expose metadata (e.g. GitHub's
    MCP server) are detected by host and given hardcoded GitHub endpoints.
    """
    base = server.url.rstrip("/")
    host = urlparse(base).netloc.lower()

    # GitHub's remote MCP server does not publish OAuth metadata and does not
    # support dynamic client registration. It authenticates against GitHub's
    # own OAuth endpoints and requires a ``resource`` parameter equal to the
    # MCP server URL so the issued token is scoped to that resource.
    if "githubcopilot.com" in host:
        return {
            "issuer": "https://github.com",
            "authorization_endpoint": "https://github.com/login/oauth/authorize",
            "token_endpoint": "https://github.com/login/oauth/access_token",
            "resource": base + "/",
            "scopes_supported": ["read:org", "repo", "workflow"],
            "no_dynamic_registration": True,
        }

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


def _static_creds(server: MCPServer) -> tuple[Optional[str], Optional[str]]:
    """Return pre-registered client credentials stored on the server row."""
    if server.oauth_client_id:
        return server.oauth_client_id, server.oauth_client_secret
    return None, None


def _stored_creds(server: MCPServer) -> tuple[Optional[str], Optional[str]]:
    """Return client credentials obtained from a previous DCR exchange."""
    stored = decrypt_json(server.oauth_encrypted) or {}
    if stored.get("client_id"):
        return stored["client_id"], stored.get("client_secret")
    return None, None


async def _try_dcr(meta: dict, redirect_uri: str) -> Optional[tuple[str, Optional[str]]]:
    """Attempt dynamic client registration; return (client_id, secret) or None.

    Returns ``None`` (without raising) when the provider does not advertise a
    registration endpoint or the registration fails — callers then fall back
    to static/pre-registered credentials.
    """
    reg_ep = meta.get("registration_endpoint")
    if not reg_ep:
        return None
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        try:
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
        except Exception:
            return None
        if r.status_code >= 400:
            return None
        data = r.json()
        return data.get("client_id"), data.get("client_secret")


async def _register_client(server: MCPServer, meta: dict, redirect_uri: str) -> tuple[str, Optional[str]]:
    """Resolve a client id/secret.

    Strategy:
      1. **Dynamic Client Registration** when the provider advertises a
         ``registration_endpoint`` (i.e. DCR is supported). The freshly
         registered client is persisted for reuse.
      2. **Static credentials** stored on the server row (``oauth_client_id`` /
         ``oauth_client_secret``) — used for providers without DCR, or as a
         fallback when DCR is unavailable.
      3. Previously-DCR'd credentials cached in ``oauth_encrypted``.

    If neither DCR nor static creds are available, a clear 400 is raised.
    """
    # 1. DCR when supported.
    if meta.get("registration_endpoint") and not meta.get("no_dynamic_registration"):
        dcr = await _try_dcr(meta, redirect_uri)
        if dcr and dcr[0]:
            client_id, client_secret = dcr
            stored = decrypt_json(server.oauth_encrypted) or {}
            blob = {**(stored or {}), "client_id": client_id, "client_secret": client_secret}
            server.oauth_encrypted = encrypt_json(blob)
            return client_id, client_secret

    # 2. Static pre-registered credentials.
    sid, ssec = _static_creds(server)
    if sid:
        return sid, ssec

    # 3. Previously cached DCR credentials.
    cid, csec = _stored_creds(server)
    if cid:
        return cid, csec

    # Nothing available.
    if meta.get("no_dynamic_registration") or not meta.get("registration_endpoint"):
        raise HTTPException(
            400,
            "This provider does not support automatic client registration. "
            "Register an OAuth App and supply its client_id/client_secret on the MCP server.",
        )
    raise HTTPException(502, "OAuth client registration failed: no client credentials available")


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
    client_id, client_secret = await _register_client(server, meta, redirect_uri)
    await db.commit()

    verifier, challenge = _gen_pkce()
    state = secrets.token_urlsafe(24)
    resource = meta.get("resource")
    _STATE[state] = {
        "server_id": server_id,
        "user_id": current_user.id,
        "verifier": verifier,
        "token_endpoint": meta.get("token_endpoint"),
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "resource": resource,
    }

    scopes = server.oauth_scopes or " ".join(meta.get("scopes_supported") or ["openid"])
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "scope": scopes,
    }
    # GitHub's MCP server requires the token to be scoped to the resource.
    if resource:
        params["resource"] = resource
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
    # GitHub (and some others) require the same resource parameter used during
    # authorization, plus the client secret for confidential clients.
    if flow.get("resource"):
        data["resource"] = flow["resource"]
    if flow.get("client_secret"):
        data["client_secret"] = flow["client_secret"]
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
        "resource": flow.get("resource"),
        "client_id": flow.get("client_id"),
        "client_secret": flow.get("client_secret"),
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
        _rdata = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": blob.get("client_id"),
        }
        if blob.get("client_secret"):
            _rdata["client_secret"] = blob["client_secret"]
        if blob.get("resource"):
            _rdata["resource"] = blob["resource"]
        r = await client.post(token_endpoint, data=_rdata)
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
