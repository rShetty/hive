"""Agent health checking and endpoint challenge."""
import hmac
import uuid
import aiohttp
import asyncio
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from models.agent import Agent, AgentStatus


async def generate_health_check_token() -> str:
    """Generate a unique token for endpoint challenge."""
    return f"verify_{uuid.uuid4().hex[:16]}"


async def ping_agent_endpoint(
    endpoint_url: str,
    token: str,
    timeout: int = 10
) -> tuple[bool, dict]:
    """
    Ping agent's health endpoint with challenge token.
    
    Returns:
        tuple: (success, response_data)
    """
    try:
        # The health endpoint is at /agents/{id}/health (backend route that
        # echoes the token back). For a marketplace proxy URL the endpoint is
        # "/agents/{id}/invoke", so we swap "/invoke" for "/health".
        if "/agents/" in endpoint_url:
            base = endpoint_url.replace("/invoke", "")
            health_url = f"{base}/health?token={token}"
        else:
            health_url = f"{endpoint_url}/health?token={token}"

        # endpoint_url may be a relative path (managed/local agents). Resolve it
        # against the running Hive instance so aiohttp gets an absolute URL.
        if health_url.startswith("/"):
            from os import getenv
            from urllib.parse import urlparse
            if getenv("OPENCLAW_DEPLOY_MODE", "local") == "local":
                _configured = getenv("HIVE_URL") or ""
                _port = f":{urlparse(_configured).port}" if urlparse(_configured).port else ""
                _hive_base = f"http://localhost{_port}" if _port else "http://localhost:8000"
            else:
                _hive_base = getenv("MARKETPLACE_URL") or getenv("HIVE_URL") or "http://localhost:8000"
            health_url = _hive_base.rstrip("/") + health_url
        
        async with aiohttp.ClientSession() as session:
            async with session.get(health_url, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
                if response.status == 200:
                    data = await response.json()
                    # Verify token matches (timing-safe)
                    if hmac.compare_digest(data.get("token", ""), token):
                        return True, data
                    else:
                        return False, {"error": "Token mismatch"}
                else:
                    return False, {"error": f"HTTP {response.status}"}
    
    except asyncio.TimeoutError:
        return False, {"error": "Timeout"}
    except Exception as e:
        return False, {"error": str(e)}


async def perform_endpoint_challenge(
    db: AsyncSession,
    agent_id: str,
    max_retries: int = 15
) -> bool:
    """
    Perform endpoint challenge for a pending agent.
    Updates agent status based on result.
    
    Returns:
        bool: True if challenge passed
    """
    from sqlalchemy import select
    
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = result.scalar_one_or_none()
    
    if not agent:
        return False
    
    if not agent.health_check_token:
        agent.health_check_token = await generate_health_check_token()
        await db.commit()
    
    # Try challenge
    for attempt in range(max_retries):
        success, data = await ping_agent_endpoint(
            agent.endpoint_url,
            agent.health_check_token
        )
        import logging as _log
        _log.getLogger("hive").warning("endpoint_challenge attempt %s url=%s ok=%s data=%s",
                                       attempt, agent.endpoint_url, success, str(data)[:120])

        if success:
            agent.status = AgentStatus.ACTIVE.value
            agent.last_health_check = datetime.now(timezone.utc)
            await db.commit()
            return True
        
        if attempt < max_retries - 1:
            await asyncio.sleep(3)
    
    # Challenge failed
    agent.status = AgentStatus.ERROR.value
    await db.commit()
    return False


async def update_agent_status_from_heartbeat(db: AsyncSession, agent_id: str):
    """Update agent status based on last_seen timestamp."""
    from sqlalchemy import select
    
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = result.scalar_one_or_none()
    
    if not agent:
        return
    
    current_status = agent.calculate_status()
    agent.status = current_status.value
    await db.commit()
