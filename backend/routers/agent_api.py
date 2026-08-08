"""Agent-only API routes (registration, heartbeat)."""
import hmac
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, Body, Depends, HTTPException, Request, status, Header, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from database import get_db
from models.agent import Agent, AgentStatus, AgentType
from models.agent_api_key import (
    AgentApiKey,
    SCOPE_ALL,
    SCOPE_HEARTBEAT,
    SCOPE_DELEGATE,
    SCOPE_COMPLETE,
    SCOPE_PROFILE_READ,
    SCOPE_PROFILE_WRITE,
    ALL_SCOPES,
)
from models.skill import Skill
from models.agent_skill import AgentSkill
from models.user import User
from schemas import (
    AgentRegistrationResponse,
    AgentHeartbeatRequest,
    AgentHeartbeatResponse,
    AgentCreate,
    AgentProfileUpdate,
    HealthCheckResponse,
    VisibilityUpdate
)
from auth import get_password_hash, get_current_active_user
from services.health_checker import generate_health_check_token
from services.skill_catalog import get_skill_by_name
from services.skill_discovery import discover_and_sync_skills

router = APIRouter(prefix="/api/agent", tags=["agent-api"])

# Scopes granted by the key used in the current request. Master key → ["*"].
# Populated by get_agent_from_api_key; read by require_scopes().
_current_scopes: ContextVar[list[str]] = ContextVar("current_scopes", default=["*"])
# True only when the request authenticated with the agent's master key.
_current_key_is_master: ContextVar[bool] = ContextVar("current_key_is_master", default=False)


def get_current_scopes() -> list[str]:
    return _current_scopes.get()


async def get_agent_from_api_key(
    x_api_key: str = Header(..., alias="X-API-Key"),
    db: AsyncSession = Depends(get_db)
) -> Agent:
    """
    Dependency to get agent from API key header.

    Accepts either the agent's master key (bcrypt-verified against
    Agent.api_key_hash, grants all scopes) or a scoped key (bcrypt-verified
    against AgentApiKey.key_hash, grants only its listed scopes).

    Uses a stored key-prefix to narrow the candidate set to ≈1 row before
    running the expensive bcrypt verify, giving O(1) amortised lookup.
    """
    from auth import verify_password

    prefix = x_api_key[:16]

    # 1. Try the master key.
    result = await db.execute(
        select(Agent).where(Agent.api_key_prefix == prefix)
    )
    for agent in result.scalars().all():
        if verify_password(x_api_key, agent.api_key_hash):
            _current_scopes.set([SCOPE_ALL])
            _current_key_is_master.set(True)
            return agent

    # 2. Fall back to scoped keys.
    result = await db.execute(
        select(AgentApiKey).where(AgentApiKey.key_prefix == prefix)
    )
    for scoped in result.scalars().all():
        if scoped.revoked:
            continue
        if verify_password(x_api_key, scoped.key_hash):
            agent_result = await db.execute(
                select(Agent).where(Agent.id == scoped.agent_id)
            )
            agent = agent_result.scalar_one_or_none()
            if agent is None:
                continue
            scoped.last_used = datetime.now(timezone.utc)
            await db.commit()
            _current_scopes.set(list(scoped.scopes or []))
            _current_key_is_master.set(False)
            return agent

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid API key"
    )


def require_scopes(*required: str):
    """FastAPI dependency factory: require any of ``required`` scopes.

    Master key (scopes == ["*"]) always passes. Scoped keys must hold at least
    one of the required scopes (or "*").
    """
    async def _check(agent: Agent = Depends(get_agent_from_api_key)):
        scopes = set(get_current_scopes())
        if SCOPE_ALL in scopes:
            return agent
        if not any(s in scopes for s in required):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required scope(s): {', '.join(required)}",
            )
        return agent
    return _check


async def require_master_key(agent: Agent = Depends(get_agent_from_api_key)) -> Agent:
    """Dependency: only the agent's master key may call this endpoint."""
    if not _current_key_is_master.get():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Master API key required for this operation",
        )
    return agent


@router.post("/register", response_model=AgentRegistrationResponse)
async def register_agent(
    agent_data: AgentCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Register a new agent (BYOA or managed).

    External agents (BYOA) supply their own `endpoint_url` and
    `agent_type="external"`.  Skills can be referenced by ID or by
    machine name (e.g. "terminal", "web_extract").

    Returns the FULL API key — save it immediately; it won’t be shown again.
    """
    import secrets
    from services.agent_keys import new_signing_fields

    api_key = f"am-{secrets.token_urlsafe(32)}"
    api_key_hash = get_password_hash(api_key)
    health_check_token = await generate_health_check_token()
    slug = agent_data.slug or Agent.generate_slug(agent_data.name)

    # Per-agent Ed25519 keypair for verifiable callback signing. The private key
    # is returned ONCE below; only the public key is stored.
    signing_fields, private_pem = new_signing_fields()

    # Determine agent type
    agent_type = agent_data.agent_type or AgentType.MANAGED.value
    is_external = agent_type == AgentType.EXTERNAL.value

    # For external agents an endpoint_url is required
    if is_external and not agent_data.endpoint_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="endpoint_url is required for external (BYOA) agents",
        )

    agent = Agent(
        name=agent_data.name,
        description=agent_data.description,
        slug=slug,
        avatar_url=agent_data.avatar_url,
        capabilities=agent_data.capabilities or [],
        tags=agent_data.tags or [],
        agent_type=agent_type,
        api_key_prefix=api_key[:16],
        api_key_hash=api_key_hash,
        endpoint_url=agent_data.endpoint_url or f"/agents/placeholder/invoke",
        status=AgentStatus.ACTIVE.value if is_external else AgentStatus.PENDING.value,
        health_check_token=health_check_token,
        owner_id=current_user.id,
        version="1.0.0",
        **signing_fields,
    )
    
    db.add(agent)
    await db.commit()
    await db.refresh(agent)

    # Fix placeholder endpoint for managed agents
    if not is_external:
        agent.endpoint_url = f"/agents/{agent.id}/invoke"
        await db.commit()

    # ---- Resolve skills by ID *and* by name ----
    resolved_skill_ids: list[str] = list(agent_data.skill_ids or [])

    for skill_name in (agent_data.skill_names or []):
        skill = await get_skill_by_name(db, skill_name)
        if skill and skill.id not in resolved_skill_ids:
            resolved_skill_ids.append(skill.id)

    for skill_id in resolved_skill_ids:
        result = await db.execute(select(Skill).where(Skill.id == skill_id))
        skill = result.scalar_one_or_none()
        if skill:
            config = (agent_data.skill_configs or {}).get(skill_id, {})
            db.add(AgentSkill(agent_id=agent.id, skill_id=skill_id, config=config))

    await db.commit()

    print(f"🔑 Agent registered: {agent.name} (ID: {agent.id}, type: {agent_type})")

    return {
        "agent_id": agent.id,
        "api_key": api_key,
        "health_check_endpoint": f"/agents/{agent.id}/health",
        "health_check_token": health_check_token,
        "status": agent.status,
        "signing_key_id": signing_fields["signing_key_id"],
        "signing_private_key": private_pem,
    }


@router.post("/heartbeat", response_model=AgentHeartbeatResponse)
async def agent_heartbeat(
    agent: Agent = Depends(require_scopes(SCOPE_HEARTBEAT)),
    db: AsyncSession = Depends(get_db),
    heartbeat: Optional[AgentHeartbeatRequest] = Body(default=None)
):
    """
    Agent heartbeat - updates last_seen timestamp.

    Accepts an optional JSON body with `ready: bool` (default true) so agents
    can signal they are busy and should not receive new delegations.
    """
    agent.last_seen = datetime.now(timezone.utc)
    agent.status = AgentStatus.ACTIVE.value
    agent.ready = heartbeat.ready if heartbeat is not None else True
    await db.commit()

    print(f"❤️‍🩹 Heartbeat: {agent.name} (ID: {agent.id}) - ready={agent.ready}")

    return AgentHeartbeatResponse(
        status="active",
        message="Heartbeat received",
        ready=agent.ready
    )


@router.get("/me")
async def get_agent_profile(
    agent: Agent = Depends(require_scopes(SCOPE_PROFILE_READ)),
    db: AsyncSession = Depends(get_db)
):
    """Get current agent's profile."""
    # Eager-load skill relationship to avoid MissingGreenlet in async context
    result = await db.execute(
        select(AgentSkill)
        .options(selectinload(AgentSkill.skill))
        .where(AgentSkill.agent_id == agent.id)
    )
    agent_skills = result.scalars().all()

    return {
        "id": agent.id,
        "name": agent.name,
        "slug": agent.slug,
        "avatar_url": agent.avatar_url,
        "capabilities": agent.capabilities or [],
        "tags": agent.tags or [],
        "description": agent.description,
        "status": agent.status,
        "ready": agent.ready if agent.ready is not None else True,
        "endpoint_url": agent.endpoint_url,
        "skills": [
            {
                "id": askill.skill.id,
                "name": askill.skill.name,
                "display_name": askill.skill.display_name,
            }
            for askill in agent_skills
            if askill.skill is not None
        ],
    }


@router.put("/me")
async def update_agent_profile(
    agent_update: AgentProfileUpdate,
    agent: Agent = Depends(require_scopes(SCOPE_PROFILE_WRITE)),
    db: AsyncSession = Depends(get_db)
):
    """Update current agent's profile."""
    for field, value in agent_update.model_dump(exclude_unset=True).items():
        setattr(agent, field, value)
    
    await db.commit()
    await db.refresh(agent)
    
    return {
        "id": agent.id,
        "name": agent.name,
        "slug": agent.slug,
        "avatar_url": agent.avatar_url,
        "capabilities": agent.capabilities or [],
        "tags": agent.tags or [],
        "description": agent.description
    }


@router.put("/visibility")
async def update_agent_visibility(
    is_public: bool | None = Query(default=None, description="Make agent public/private"),
    visibility: VisibilityUpdate | None = None,
    agent: Agent = Depends(require_scopes(SCOPE_PROFILE_WRITE)),
    db: AsyncSession = Depends(get_db)
):
    """
    Update agent's marketplace visibility and settings.
    Agents can make themselves public/private.

    `is_public` may be supplied either as a query parameter
    (e.g. `PUT /api/agent/visibility?is_public=true`) or in the JSON body.
    """
    is_public_final = is_public
    marketplace_description = None
    pricing_model = None

    if visibility is not None:
        if is_public_final is None:
            is_public_final = visibility.is_public
        marketplace_description = visibility.marketplace_description
        pricing_model = visibility.pricing_model

    if is_public_final is None:
        raise HTTPException(
            status_code=422,
            detail="is_public is required (query param or JSON body)",
        )

    agent.is_public = is_public_final

    if marketplace_description is not None:
        agent.marketplace_description = marketplace_description

    if pricing_model is not None:
        # Convert Pydantic model to dict for JSON storage
        agent.pricing_model = pricing_model.model_dump()
    
    await db.commit()
    await db.refresh(agent)
    
    return {
        "id": agent.id,
        "is_public": agent.is_public,
        "marketplace_description": agent.marketplace_description,
        "pricing_model": agent.pricing_model,
        "message": f"Agent is now {'public' if agent.is_public else 'private'}"
    }


# ---- Distributed rate limiter for credential recovery ----
# Uses the shared Redis-backed kvstore so limits are enforced across instances
# and survive restarts. Falls back to in-memory in dev.
from services import kvstore
_RATE_LIMIT_WINDOW = 300  # 5 minutes
_RATE_LIMIT_MAX = 5  # max attempts per window

# Self-registration rate limits
_SELF_REG_LIMIT_WINDOW = 3600  # 1 hour
_SELF_REG_LIMIT_MAX = 10  # max 10 registrations per IP per hour


async def _check_rate_limit(key: str) -> None:
    count, allowed = await kvstore.fixed_window_count(
        f"rl:{key}", _RATE_LIMIT_WINDOW, _RATE_LIMIT_MAX
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many recovery attempts. Try again later.",
        )


@router.post("/recover-credentials")
async def recover_credentials(
    agent_id: str,
    health_check_token: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    Recover agent credentials using health check token.
    This is a one-time recovery - generates a NEW API key.
    """
    await _check_rate_limit(f"{request.client.host}:{agent_id}")

    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = result.scalar_one_or_none()
    
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found"
        )
    
    if not hmac.compare_digest(agent.health_check_token or "", health_check_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid health check token"
        )
    
    # Generate new API key (old one is lost forever)
    import secrets
    new_api_key = f"am-{secrets.token_urlsafe(32)}"
    agent.api_key_prefix = new_api_key[:16]
    agent.api_key_hash = get_password_hash(new_api_key)
    
    # Generate new health check token too (for security)
    new_health_token = await generate_health_check_token()
    agent.health_check_token = new_health_token
    
    await db.commit()
    
    print(f"🔄 Credentials recovered for agent: {agent.name} (ID: {agent.id})")
    
    return {
        "agent_id": agent.id,
        "api_key": new_api_key,
        "health_check_token": new_health_token,
        "message": "New credentials generated. Save these immediately - they won't be shown again!"
    }


@router.post("/rotate-signing-key")
async def rotate_signing_key(
    agent: Agent = Depends(require_master_key),
    db: AsyncSession = Depends(get_db),
):
    """Rotate the agent's Ed25519 signing keypair.

    Generates a fresh keypair, stores the new public key + key_id, and returns
    the new private key ONCE. The previous key immediately stops being valid
    for callback verification. Existing in-flight callbacks signed with the old
    key will be rejected — callers should drain pending work before rotating.
    """
    from services.agent_keys import new_signing_fields

    signing_fields, private_pem = new_signing_fields()
    agent.signing_key_id = signing_fields["signing_key_id"]
    agent.signing_public_key = signing_fields["signing_public_key"]
    agent.signing_key_created_at = signing_fields["signing_key_created_at"]
    await db.commit()

    print(f"🔑 Signing key rotated for agent: {agent.name} (ID: {agent.id})")

    return {
        "agent_id": agent.id,
        "signing_key_id": signing_fields["signing_key_id"],
        "signing_private_key": private_pem,
        "message": "New signing key generated. Save the private key immediately - it won't be shown again!",
    }


# ── Scoped API key management ────────────────────────────────────────────────

@router.post("/api-keys")
async def create_scoped_api_key(
    body: dict = Body(...),
    agent: Agent = Depends(require_master_key),
    db: AsyncSession = Depends(get_db),
):
    """Issue a new scoped API key for this agent.

    Body: ``{"name": "ci-runner", "scopes": ["heartbeat", "complete"]}``.
    The full key is returned ONCE (like the master key at registration).
    Only the agent's master key may call this.
    """
    import secrets
    name = (body or {}).get("name") or "scoped-key"
    requested = (body or {}).get("scopes") or []
    invalid = [s for s in requested if s not in ALL_SCOPES]
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown scope(s): {', '.join(invalid)}. Valid: {sorted(ALL_SCOPES)}",
        )
    if not requested:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one scope is required",
        )

    raw_key = f"am-{secrets.token_urlsafe(32)}"
    scoped = AgentApiKey(
        agent_id=agent.id,
        name=name[:100],
        key_prefix=raw_key[:16],
        key_hash=get_password_hash(raw_key),
        scopes=list(requested),
    )
    db.add(scoped)
    await db.commit()
    await db.refresh(scoped)

    print(f"🎫 Scoped key issued for agent {agent.name}: name={name} scopes={requested}")

    return {
        "id": scoped.id,
        "name": scoped.name,
        "scopes": scoped.scopes,
        "api_key": raw_key,
        "message": "Save this key immediately - it won't be shown again!",
    }


@router.get("/api-keys")
async def list_scoped_api_keys(
    agent: Agent = Depends(require_master_key),
    db: AsyncSession = Depends(get_db),
):
    """List this agent's scoped API keys (metadata only; no secrets)."""
    result = await db.execute(
        select(AgentApiKey).where(AgentApiKey.agent_id == agent.id)
    )
    return {
        "agent_id": agent.id,
        "keys": [
            {
                "id": k.id,
                "name": k.name,
                "scopes": k.scopes or [],
                "revoked": k.revoked,
                "last_used": k.last_used.isoformat() if k.last_used else None,
                "created_at": k.created_at.isoformat() if k.created_at else None,
            }
            for k in result.scalars().all()
        ],
    }


@router.delete("/api-keys/{key_id}")
async def revoke_scoped_api_key(
    key_id: str,
    agent: Agent = Depends(require_master_key),
    db: AsyncSession = Depends(get_db),
):
    """Revoke a scoped API key. The key immediately stops working."""
    result = await db.execute(
        select(AgentApiKey).where(
            AgentApiKey.id == key_id, AgentApiKey.agent_id == agent.id
        )
    )
    scoped = result.scalar_one_or_none()
    if not scoped:
        raise HTTPException(status_code=404, detail="Scoped key not found")
    scoped.revoked = True
    await db.commit()
    return {"id": scoped.id, "revoked": True}


@router.post("/discover-skills")
async def discover_skills(
    agent: Agent = Depends(require_scopes(SCOPE_PROFILE_WRITE)),
    db: AsyncSession = Depends(get_db)
):
    """
    Discover skills from the agent's endpoint.
    
    Calls the agent's endpoint_url/.well-known/skills to get available skills.
    Auto-creates Skill records for unknown skills and links them to the agent.
    
    The agent should respond with:
    [
        {"name": "terminal", "display_name": "Terminal", "description": "..."},
        {"name": "web_extract", "display_name": "Web Extract", ...}
    ]
    """
    result = await discover_and_sync_skills(agent, db)
    return result


@router.get("/skills")
async def get_discovered_skills(
    agent: Agent = Depends(require_scopes(SCOPE_PROFILE_READ)),
    db: AsyncSession = Depends(get_db)
):
    """Get the agent's current discovered skills."""
    result = await db.execute(
        select(AgentSkill)
        .where(AgentSkill.agent_id == agent.id)
    )
    agent_skills = result.scalars().all()
    
    skills_list = []
    for askill in agent_skills:
        # Load the skill relationship
        skill_result = await db.execute(
            select(Skill).where(Skill.id == askill.skill_id)
        )
        skill = skill_result.scalar_one_or_none()
        if skill:
            skills_list.append({
                "id": skill.id,
                "name": skill.name,
                "display_name": skill.display_name,
                "description": skill.description,
                "tier": skill.tier,
                "category": skill.category,
                "config": askill.config
            })
    
    return {
        "agent_id": agent.id,
        "skills": skills_list,
        "total": len(skills_list)
    }



