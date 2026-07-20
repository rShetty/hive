"""Agent-only API routes (registration, heartbeat)."""
import hmac
from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, Body, Depends, HTTPException, Request, status, Header, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from database import get_db
from models.agent import Agent, AgentStatus, AgentType
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


async def get_agent_from_api_key(
    x_api_key: str = Header(..., alias="X-API-Key"),
    db: AsyncSession = Depends(get_db)
) -> Agent:
    """
    Dependency to get agent from API key header.

    Uses a stored key-prefix to narrow the candidate set to ≈1 row before
    running the expensive bcrypt verify, giving O(1) amortised lookup.
    """
    from auth import verify_password

    # Keys are formatted "am-<token>" — the prefix is the first 16 chars.
    prefix = x_api_key[:16]
    result = await db.execute(
        select(Agent).where(Agent.api_key_prefix == prefix)
    )
    candidates = result.scalars().all()

    for agent in candidates:
        if verify_password(x_api_key, agent.api_key_hash):
            return agent

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid API key"
    )


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

    api_key = f"am-{secrets.token_urlsafe(32)}"
    api_key_hash = get_password_hash(api_key)
    health_check_token = await generate_health_check_token()
    slug = agent_data.slug or Agent.generate_slug(agent_data.name)

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
    }


@router.post("/heartbeat", response_model=AgentHeartbeatResponse)
async def agent_heartbeat(
    agent: Agent = Depends(get_agent_from_api_key),
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
    agent: Agent = Depends(get_agent_from_api_key),
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
    agent: Agent = Depends(get_agent_from_api_key),
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
    agent: Agent = Depends(get_agent_from_api_key),
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


# ---- In-memory rate limiter for credential recovery ----
import time as _time
_recovery_attempts: dict[str, list[float]] = {}  # key -> list of timestamps
_RATE_LIMIT_WINDOW = 300  # 5 minutes
_RATE_LIMIT_MAX = 5  # max attempts per window

# Self-registration rate limits
_SELF_REG_LIMIT_WINDOW = 3600  # 1 hour
_SELF_REG_LIMIT_MAX = 10  # max 10 registrations per IP per hour

def _check_rate_limit(key: str) -> None:
    now = _time.time()
    attempts = _recovery_attempts.get(key, [])
    # Prune old entries
    attempts = [t for t in attempts if now - t < _RATE_LIMIT_WINDOW]
    if len(attempts) >= _RATE_LIMIT_MAX:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many recovery attempts. Try again later.",
        )
    attempts.append(now)
    _recovery_attempts[key] = attempts


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
    _check_rate_limit(f"{request.client.host}:{agent_id}")

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


@router.post("/discover-skills")
async def discover_skills(
    agent: Agent = Depends(get_agent_from_api_key),
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
    agent: Agent = Depends(get_agent_from_api_key),
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



