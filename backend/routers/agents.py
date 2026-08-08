"""Public agent routes (browsing, discovery)."""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from database import get_db
from models.agent import Agent, AgentStatus
from models.skill import Skill
from models.agent_skill import AgentSkill
from schemas import AgentResponse, AgentDetailResponse, AgentFilter
from auth import get_current_active_user, get_current_user, get_current_admin_user
from models.user import User

router = APIRouter(prefix="/api/agents", tags=["agents"])


@router.get("/stats/overview")
async def get_agent_stats(db: AsyncSession = Depends(get_db)):
    """Get marketplace statistics."""
    # Total agents
    total_result = await db.execute(select(func.count(Agent.id)))
    total = total_result.scalar()
    
    # Active agents (online or idle)
    active_result = await db.execute(
        select(func.count(Agent.id))
        .where(Agent.status.in_([AgentStatus.ACTIVE.value, AgentStatus.IDLE.value]))
    )
    active = active_result.scalar()
    
    return {
        "total_agents": total,
        "active_agents": active,
        "offline_agents": total - active
    }


@router.get("")
async def list_agents(
    status: Optional[str] = None,
    skill_id: Optional[str] = None,
    owner_id: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db)
):
    """List all agents with optional filtering."""
    query = select(Agent).options(selectinload(Agent.skills).selectinload(AgentSkill.skill))
    
    # Apply filters
    if status:
        query = query.where(Agent.status == status)
    else:
        # Default: exclude pending/error agents from public view
        query = query.where(Agent.status.in_([
            AgentStatus.ACTIVE.value,
            AgentStatus.IDLE.value,
            AgentStatus.OFFLINE.value
        ]))
    
    if owner_id:
        query = query.where(Agent.owner_id == owner_id)
    
    if search:
        search_filter = f"%{search}%"
        query = query.where(
            (Agent.name.ilike(search_filter)) | 
            (Agent.description.ilike(search_filter))
        )
    
    if skill_id:
        # Join with AgentSkill to filter by skill
        query = query.join(AgentSkill).where(AgentSkill.skill_id == skill_id)
    
    # Get total count before pagination
    count_query = select(func.count()).select_from(query.alias())
    total_result = await db.execute(count_query)
    total_count = total_result.scalar()
    
    # Order by most recently active
    query = query.order_by(Agent.last_seen.desc().nullslast())
    query = query.limit(limit).offset(offset)
    
    result = await db.execute(query)
    agents = result.scalars().unique().all()
    
    # Fetch owner info for each agent
    items = []
    for agent in agents:
        agent_dict = AgentResponse.model_validate(agent).model_dump()
        if agent.owner_id:
            owner_result = await db.execute(select(User).where(User.id == agent.owner_id))
            owner = owner_result.scalar_one_or_none()
            if owner:
                agent_dict["owner_name"] = owner.name
                agent_dict["owner_email"] = owner.email
        items.append(agent_dict)
    
    return {
        "items": items,
        "total": total_count,
        "limit": limit,
        "offset": offset
    }


@router.get("/{agent_id}", response_model=AgentDetailResponse)
async def get_agent(agent_id: str, db: AsyncSession = Depends(get_db)):
    """Get detailed information about a specific agent."""
    result = await db.execute(
        select(Agent)
        .options(selectinload(Agent.skills).selectinload(AgentSkill.skill))
        .where(Agent.id == agent_id)
    )
    agent = result.scalar_one_or_none()
    
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found"
        )
    
    # Update calculated status
    agent.status = agent.calculate_status().value
    
    # Include owner info
    agent_dict = AgentDetailResponse.model_validate(agent).model_dump()
    if agent.owner_id:
        owner_result = await db.execute(select(User).where(User.id == agent.owner_id))
        owner = owner_result.scalar_one_or_none()
        if owner:
            agent_dict["owner_name"] = owner.name
            agent_dict["owner_email"] = owner.email
    
    return agent_dict


@router.get("/{agent_id}/skills", response_model=List[dict])
async def get_agent_skills(agent_id: str, db: AsyncSession = Depends(get_db)):
    """Get skills for a specific agent."""
    result = await db.execute(
        select(AgentSkill)
        .options(selectinload(AgentSkill.skill))
        .where(AgentSkill.agent_id == agent_id)
    )
    agent_skills = result.scalars().all()
    
    return [
        {
            "id": askill.skill.id,
            "name": askill.skill.name,
            "display_name": askill.skill.display_name,
            "description": askill.skill.description,
            "tier": askill.skill.tier,
            "category": askill.skill.category
        }
        for askill in agent_skills
    ]


@router.get("/{agent_id}/card")
async def get_agent_card(agent_id: str, db: AsyncSession = Depends(get_db)):
    """
    Return an A2A-compatible AgentCard for the given agent.

    Follows the Google Agent2Agent (A2A) protocol specification:
    https://google.github.io/A2A

    The card is intended to be machine-readable so other agents can discover
    this agent's capabilities, authentication requirements, and delegation endpoint.
    """
    result = await db.execute(
        select(Agent)
        .options(selectinload(Agent.skills).selectinload(AgentSkill.skill))
        .where(Agent.id == agent_id)
    )
    agent = result.scalar_one_or_none()

    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    if not agent.is_public:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Agent is not public")

    marketplace_url = __import__("os").getenv("MARKETPLACE_URL", "http://localhost:8000")
    agent_base_url = agent.endpoint_url or f"{marketplace_url}/agents/{agent.id}"

    skills_cards = []
    for askill in agent.skills:
        if askill.skill:
            skills_cards.append({
                "id": askill.skill.name,
                "name": askill.skill.display_name,
                "description": askill.skill.description,
                "tags": [askill.skill.category, askill.skill.tier],
                "examples": [],
            })

    card = {
        # A2A required fields
        "name": agent.name,
        "description": agent.description or agent.marketplace_description or "",
        "url": agent_base_url,
        "version": agent.version or "1.0.0",
        # W3C DID — portable, resolvable agent identity
        "id": f"did:hive:{agent.id}",
        # Authentication — Hive-mediated access (dashboard proxy, delegation
        # API) uses Bearer JWTs; agents self-identify to the marketplace via an
        # X-API-Key header. Both schemes are advertised so A2A clients can pick.
        "authentication": {
            "schemes": ["Bearer", "ApiKey"],
            "credentials": None,
        },
        # Streaming supported via SSE
        "capabilities": {
            "streaming": True,
            "pushNotifications": True,
            "stateTransitionHistory": False,
        },
        # Skills / tools this agent exposes
        "skills": skills_cards,
        # Hive-specific extensions
        "x-hive": {
            "agent_id": agent.id,
            "did": f"did:hive:{agent.id}",
            "slug": agent.slug,
            "avatar_url": agent.avatar_url,
            "tags": agent.tags or [],
            "capabilities": agent.capabilities or [],
            "status": agent.status,
            "pricing_model": agent.pricing_model,
            "delegation_endpoint": f"{marketplace_url}/api/delegate",
            "marketplace_url": f"{marketplace_url}/api/marketplace/agents/{agent.id}",
            "auth": {
                "marketplace_proxy": "Bearer JWT (hive_token cookie / Authorization header)",
                "agent_self": "X-API-Key header (issued once at registration)",
            },
        },
    }

    return card


@router.get("/{agent_id}/credentials")
async def get_agent_credentials(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Return W3C Verifiable Credentials for this agent.

    Hive, as the platform authority, issues VCs attesting to:
      - Ownership (who owns this agent)
      - Agent metadata (type, capabilities, status)
      - Marketplace listing (public visibility + pricing)

    The credentials are signed with Hive's platform Ed25519 key and can be
    verified by any third party using the public key at
    ``/.well-known/hive-identity``.
    """
    result = await db.execute(
        select(Agent)
        .options(selectinload(Agent.skills).selectinload(AgentSkill.skill))
        .where(Agent.id == agent_id)
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    from services.credentials import issue_all_credentials
    creds = await issue_all_credentials(agent, db)
    return {
        "agent_id": agent_id,
        "did": f"did:hive:{agent_id}",
        "credentials": creds,
    }


@router.get("/{agent_id}/did-document")
async def get_agent_did_document(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Return the W3C DID Document for this agent.

    Shortcut for ``GET /.well-known/did/did:hive:{agent_id}``.
    """
    from services.did import resolve_did
    did = f"did:hive:{agent_id}"
    doc = await resolve_did(did, db)
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return doc


@router.post("/{agent_id}/discover-skills")
async def trigger_skill_discovery(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Trigger skill discovery for an agent.
    
    Queries the agent's endpoint to discover its available skills.
    Only the agent owner or admin can trigger this.
    """
    from services.skill_discovery import discover_and_sync_skills
    
    result = await db.execute(
        select(Agent)
        .options(selectinload(Agent.skills))
        .where(Agent.id == agent_id)
    )
    agent = result.scalar_one_or_none()
    
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found"
        )
    
    # Check ownership
    if agent.owner_id != current_user.id and not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to discover skills for this agent"
        )
    
    discovery_result = await discover_and_sync_skills(agent, db)
    
    return {
        "agent_id": agent.id,
        "agent_name": agent.name,
        **discovery_result
    }


# ── Admin: HMAC → Ed25519 migration status ──────────────────────────────────

@router.get("/admin/migration-status")
async def get_migration_status(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Admin: check how many agents still need Ed25519 key migration.

    Agents with ``signing_key_id = NULL`` were registered before the
    Ed25519 identity system and still rely on the legacy shared HMAC for
    callback signing. Use the migration tool (``services/migrate_keys.py``)
    to issue them keypairs.
    """
    total_result = await db.execute(select(func.count(Agent.id)))
    total = total_result.scalar()

    legacy_result = await db.execute(
        select(func.count(Agent.id)).where(Agent.signing_key_id.is_(None))
    )
    legacy = legacy_result.scalar()

    migrated = (total or 0) - (legacy or 0)
    pct = (migrated / total * 100) if total else 100

    return {
        "total_agents": total,
        "migrated": migrated,
        "legacy_hmac_only": legacy,
        "migration_percentage": round(pct, 1),
        "can_deprecate_hmac": legacy == 0,
        "message": (
            "All agents have Ed25519 keys. HIVE_SIGNING_SECRET can be deprecated."
            if legacy == 0
            else f"{legacy} agent(s) still rely on legacy HMAC. Run the migration tool."
        ),
    }
