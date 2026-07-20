"""MCP server registry + per-agent access grants.

Users register MCP servers and explicitly grant individual agents access.
"""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from database import get_db
from models.mcp import MCPServer, AgentMCPAccess
from models.agent import Agent
from models.user import User
from schemas import (
    MCPServerCreate, MCPServerUpdate, MCPServerResponse,
    AgentMCPGrantRequest, AgentMCPAccessResponse,
)
from services.crypto import encrypt_json, decrypt_json
from auth import get_current_active_user

router = APIRouter(prefix="/api/mcp-servers", tags=["mcp-servers"])


def _validate_url(url: str):
    from urllib.parse import urlparse
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MCP server URL must start with http:// or https://",
        )


def _to_response(server: MCPServer, agent_count: int = None) -> MCPServerResponse:
    return MCPServerResponse(
        id=server.id,
        owner_id=server.owner_id,
        name=server.name,
        url=server.url,
        description=server.description,
        transport=server.transport,
        auth_type=server.auth_type,
        oauth_connected=bool(server.oauth_encrypted),
        command=server.command,
        oauth_client_id=server.oauth_client_id,
        visibility=server.visibility,
        is_active=server.is_active,
        created_at=server.created_at,
        agent_count=agent_count,
    )


@router.get("", response_model=List[MCPServerResponse])
async def list_mcp_servers(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """List the caller's MCP servers with their granted-agent counts."""
    result = await db.execute(
        select(MCPServer).where(MCPServer.owner_id == current_user.id)
    )
    servers = result.scalars().all()
    out = []
    for s in servers:
        cnt = (await db.execute(
            select(func.count()).select_from(AgentMCPAccess).where(
                AgentMCPAccess.mcp_server_id == s.id
            )
        )).scalar() or 0
        out.append(_to_response(s, agent_count=cnt))
    return out


@router.post("", response_model=MCPServerResponse, status_code=status.HTTP_201_CREATED)
async def create_mcp_server(
    data: MCPServerCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    if data.transport != "stdio":
        _validate_url(data.url)
    headers_enc = encrypt_json(data.headers) if data.headers else None
    env_enc = encrypt_json(data.env) if data.env else None
    server = MCPServer(
        owner_id=current_user.id,
        name=data.name,
        url=data.url or "",
        description=data.description,
        transport=data.transport,
        auth_type=data.auth_type,
        command=data.command,
        env_encrypted=env_enc,
        headers_encrypted=headers_enc,
        oauth_client_id=data.oauth_client_id,
        oauth_client_secret=data.oauth_client_secret,
        oauth_scopes=data.oauth_scopes,
        visibility="private",
    )
    db.add(server)
    await db.commit()
    await db.refresh(server)
    return _to_response(server)


@router.get("/{server_id}", response_model=MCPServerResponse)
async def get_mcp_server(
    server_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    server = await _get_owned_server(db, server_id, current_user)
    return _to_response(server)


@router.put("/{server_id}", response_model=MCPServerResponse)
async def update_mcp_server(
    server_id: str,
    data: MCPServerUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    server = await _get_owned_server(db, server_id, current_user)
    if data.url is not None:
        _validate_url(data.url)
        server.url = data.url
    if data.name is not None:
        server.name = data.name
    if data.description is not None:
        server.description = data.description
    if data.transport is not None:
        server.transport = data.transport
    if data.auth_type is not None:
        server.auth_type = data.auth_type
    if data.command is not None:
        server.command = data.command
    if data.env is not None:
        server.env_encrypted = encrypt_json(data.env) if data.env else None
    if data.headers is not None:
        server.headers_encrypted = encrypt_json(data.headers) if data.headers else None
    if data.oauth_client_id is not None:
        server.oauth_client_id = data.oauth_client_id
    if data.oauth_client_secret is not None:
        server.oauth_client_secret = data.oauth_client_secret
    if data.oauth_scopes is not None:
        server.oauth_scopes = data.oauth_scopes
    await db.commit()
    await db.refresh(server)
    return _to_response(server)


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mcp_server(
    server_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    server = await _get_owned_server(db, server_id, current_user)
    await db.delete(server)  # cascades AgentMCPAccess
    await db.commit()


@router.get("/{server_id}/agents", response_model=List[AgentMCPAccessResponse])
async def list_server_access(
    server_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """List which agents have access to this MCP server."""
    await _get_owned_server(db, server_id, current_user)
    return await _access_rows(db, mcp_server_id=server_id)


@router.post("/{server_id}/grant", response_model=List[AgentMCPAccessResponse])
async def grant_access(
    server_id: str,
    data: AgentMCPGrantRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Grant one or more of the caller's agents access to this MCP server."""
    server = await _get_owned_server(db, server_id, current_user)

    created = []
    for agent_id in data.agent_ids:
        agent = await _get_owned_agent(db, agent_id, current_user)
        # Idempotent: reuse existing grant if present.
        existing = (await db.execute(
            select(AgentMCPAccess).where(
                AgentMCPAccess.agent_id == agent_id,
                AgentMCPAccess.mcp_server_id == server_id,
            )
        )).scalar_one_or_none()
        if existing:
            existing.enabled = True
            access = existing
        else:
            override = (data.headers or {}).get(agent_id)
            access = AgentMCPAccess(
                agent_id=agent_id,
                mcp_server_id=server_id,
                headers_encrypted=encrypt_json(override) if override else None,
                enabled=True,
            )
            db.add(access)
        await db.commit()
        await db.refresh(access)
        created.append(access)
    return [await _access_response(db, a) for a in created]


@router.post("/{server_id}/revoke", response_model=dict)
async def revoke_access(
    server_id: str,
    data: AgentMCPGrantRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Revoke access for the given agents (disables grants)."""
    await _get_owned_server(db, server_id, current_user)
    for agent_id in data.agent_ids:
        await _get_owned_agent(db, agent_id, current_user)
        existing = (await db.execute(
            select(AgentMCPAccess).where(
                AgentMCPAccess.agent_id == agent_id,
                AgentMCPAccess.mcp_server_id == server_id,
            )
        )).scalar_one_or_none()
        if existing:
            existing.enabled = False
            await db.commit()
    return {"ok": True}


@router.get("/agent/{agent_id}", response_model=List[AgentMCPAccessResponse])
async def list_agent_access(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """List MCP servers an agent has access to (owner-only)."""
    await _get_owned_agent(db, agent_id, current_user)
    return await _access_rows(db, agent_id=agent_id)


# ---- helpers ----

async def _get_owned_server(db, server_id, user) -> MCPServer:
    result = await db.execute(select(MCPServer).where(MCPServer.id == server_id))
    server = result.scalar_one_or_none()
    if not server:
        raise HTTPException(status_code=404, detail="MCP server not found")
    if server.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Not your MCP server")
    return server


async def _get_owned_agent(db, agent_id, user) -> Agent:
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Not your agent")
    return agent


async def _access_rows(db, agent_id=None, mcp_server_id=None):
    q = select(AgentMCPAccess)
    if agent_id:
        q = q.where(AgentMCPAccess.agent_id == agent_id)
    if mcp_server_id:
        q = q.where(AgentMCPAccess.mcp_server_id == mcp_server_id)
    rows = (await db.execute(q)).scalars().all()
    return [await _access_response(db, r) for r in rows]


async def _access_response(db, access: AgentMCPAccess) -> AgentMCPAccessResponse:
    server = (await db.execute(
        select(MCPServer).where(MCPServer.id == access.mcp_server_id)
    )).scalar_one_or_none()
    return AgentMCPAccessResponse(
        id=access.id,
        agent_id=access.agent_id,
        mcp_server_id=access.mcp_server_id,
        mcp_server_name=server.name if server else None,
        mcp_server_url=server.url if server else None,
        enabled=access.enabled,
        created_at=access.created_at,
    )
