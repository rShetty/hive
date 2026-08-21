"""Skill discovery service - queries agents for their available skills."""
import httpx
from typing import List, Dict, Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from models.skill import Skill
from models.agent_skill import AgentSkill
from models.agent import Agent


async def discover_agent_skills(
    agent: Agent,
    db: AsyncSession,
    timeout: float = 10.0
) -> List[Dict[str, Any]]:
    """
    Query an agent's endpoint to discover its available skills.
    
    Calls GET {endpoint_url}/.well-known/skills or /skills
    
    Returns list of skill info dicts.
    """
    if not agent.endpoint_url:
        return []
    
    # Issue #17 (SSRF): discovery contacts the agent endpoint from the Hive
    # backend and ingests its response as skill metadata. Only public
    # endpoints may be probed. (Relative endpoints are managed agents served
    # by this instance and need no check.)
    if not agent.endpoint_url.startswith("/"):
        from services.url_guard import validate_public_http_url
        try:
            base_url = validate_public_http_url(agent.endpoint_url).rstrip("/")
        except ValueError:
            return []
    else:
        base_url = agent.endpoint_url.rstrip("/")
    
    async with httpx.AsyncClient(timeout=timeout) as client:
        # Try well-known location first
        for path in ["/.well-known/skills", "/skills", "/agent/skills"]:
            try:
                url = f"{base_url}{path}"
                response = await client.get(
                    url,
                    headers={"Accept": "application/json"},
                    follow_redirects=True
                )
                
                if response.status_code == 200:
                    data = response.json()
                    # Handle different response formats
                    if isinstance(data, list):
                        return data
                    elif isinstance(data, dict) and "skills" in data:
                        return data["skills"]
                    elif isinstance(data, dict):
                        return [data]
                    
            except httpx.HTTPError:
                continue
            except Exception:
                continue
    
    return []


async def sync_agent_skills(
    agent: Agent,
    db: AsyncSession,
    discovered_skills: List[Dict[str, Any]]
) -> List[Dict[str, str]]:
    """
    Sync discovered skills to the database.

    Creates/updates AgentSkill records based on discovered skills.
    Also auto-creates Skill records if they don't exist.

    Returns a list of dicts with skill metadata (avoids lazy-load issues in
    async SQLAlchemy when the caller needs skill name / display_name).
    """
    synced: List[Dict[str, str]] = []

    for skill_info in discovered_skills:
        skill_name = skill_info.get("name") or skill_info.get("skill_name") or skill_info.get("id")

        if not skill_name:
            continue

        # Find or create skill
        result = await db.execute(select(Skill).where(Skill.name == skill_name))
        skill = result.scalar_one_or_none()

        if not skill:
            display_name = skill_info.get("display_name") or skill_name
            skill = Skill(
                name=skill_name,
                display_name=display_name,
                description=skill_info.get("description") or f"Skill: {skill_name}",
                tier=skill_info.get("tier") or "core",
                category=skill_info.get("category") or "general",
                required_env_vars=skill_info.get("required_env_vars") or [],
            )
            db.add(skill)
            await db.flush()

        # Check if agent already has this skill
        existing_result = await db.execute(
            select(AgentSkill).where(
                AgentSkill.agent_id == agent.id,
                AgentSkill.skill_id == skill.id
            )
        )
        agent_skill = existing_result.scalar_one_or_none()

        if not agent_skill:
            agent_skill = AgentSkill(
                agent_id=agent.id,
                skill_id=skill.id,
                config=skill_info.get("config") or {}
            )
            db.add(agent_skill)

        # Capture names now while the Skill ORM object is still in scope —
        # avoids lazy-loading the relationship after the session is used elsewhere.
        synced.append({"name": skill.name, "display_name": skill.display_name})

    await db.commit()
    return synced


async def discover_and_sync_skills(
    agent: Agent,
    db: AsyncSession,
    timeout: float = 10.0
) -> Dict[str, Any]:
    """
    Discover skills from agent endpoint and sync to database.

    Returns summary of what was discovered and synced.
    """
    discovered = await discover_agent_skills(agent, db, timeout)

    if not discovered:
        return {
            "discovered": 0,
            "synced": 0,
            "skills": [],
            "message": "No skills discovered. Agent may not expose a /skills endpoint."
        }

    synced = await sync_agent_skills(agent, db, discovered)

    return {
        "discovered": len(discovered),
        "synced": len(synced),
        "skills": synced,
        "message": f"Discovered {len(discovered)} skills, synced {len(synced)} to database."
    }
