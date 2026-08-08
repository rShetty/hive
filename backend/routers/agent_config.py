"""Per-agent configuration: LLM API keys, messaging integrations, skill management."""
from __future__ import annotations

import json
import os
import logging
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from auth import get_current_active_user
from database import get_db
from models.agent import Agent, AgentStatus
from models.agent_skill import AgentSkill
from models.skill import Skill
from models.user import User
from schemas import HiveBaseModel

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["agent-config"])

# ── Fernet encryption (shared with deploy.py) ─────────────────────────────────
from cryptography.fernet import Fernet

_env_key = os.getenv("ENCRYPTION_KEY")
if _env_key:
    import base64
    try:
        _fernet_key = _env_key.encode() if isinstance(_env_key, str) else _env_key
        Fernet(_fernet_key)  # validate
    except Exception:
        _fernet_key = base64.urlsafe_b64encode(_env_key.encode().ljust(32, b"\0")[:32])
else:
    _fernet_key = Fernet.generate_key()

_fernet = Fernet(_fernet_key)


def _encrypt(data: dict) -> str:
    return _fernet.encrypt(json.dumps(data).encode()).decode()


def _decrypt(ciphertext: str | None) -> dict:
    if not ciphertext:
        return {}
    try:
        return json.loads(_fernet.decrypt(ciphertext.encode()))
    except Exception:
        return {}


# ── VPS env-var settings (same as deploy.py) ─────────────────────────────────
OPENCLAW_VPS_HOST = os.getenv("OPENCLAW_VPS_HOST")
OPENCLAW_VPS_SSH_KEY_PATH = os.getenv("OPENCLAW_VPS_SSH_KEY_PATH")
OPENCLAW_VPS_SSH_USER = os.getenv("OPENCLAW_VPS_SSH_USER", "root")
OPENCLAW_VPS_SSH_PORT = int(os.getenv("OPENCLAW_VPS_SSH_PORT", "22"))
HIVE_URL = os.getenv("HIVE_URL", "http://localhost:8080")


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_owned_agent(agent_id: str, user: User, db: AsyncSession) -> Agent:
    result = await db.execute(
        select(Agent)
        .options(selectinload(Agent.skills).selectinload(AgentSkill.skill))
        .where(Agent.id == agent_id)
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Not your agent")
    return agent


async def _restart_with_config(agent: Agent, config: dict) -> dict:
    """Push updated env vars to the VPS container and force-recreate it."""
    if agent.agent_type != "openclaw":
        return {"success": True, "message": "Config saved (non-OpenClaw agent — no container restart needed)"}

    if not agent.openclaw_instance_id:
        return {"success": False, "message": "No OpenClaw instance ID recorded — redeploy the agent first"}

    if not OPENCLAW_VPS_HOST or not OPENCLAW_VPS_SSH_KEY_PATH:
        return {"success": False, "message": "VPS not configured on server"}

    if not os.path.isfile(OPENCLAW_VPS_SSH_KEY_PATH):
        return {"success": False, "message": f"SSH key not found at {OPENCLAW_VPS_SSH_KEY_PATH}"}

    # Reconstruct the raw API key for the VPS container. Stored in a dedicated
    # encrypted column (api_key_encrypted); fall back to the legacy location in
    # config_encrypted for agents registered before the column was added.
    raw_api_key = ""
    if agent.api_key_encrypted:
        raw_api_key = _decrypt(agent.api_key_encrypted).get("_hive_api_key", "")
    if not raw_api_key:
        raw_api_key = _decrypt(agent.config_encrypted).get("_hive_api_key", "")

    from services.openclaw_deployer import update_container_env
    return await update_container_env(
        vps_host=OPENCLAW_VPS_HOST,
        ssh_key_path=OPENCLAW_VPS_SSH_KEY_PATH,
        instance_id=agent.openclaw_instance_id,
        agent_name=agent.name,
        agent_id=agent.id,
        api_key=raw_api_key,
        port=agent.internal_port or 9000,
        config_env=config,
        ssh_user=OPENCLAW_VPS_SSH_USER,
        ssh_port=OPENCLAW_VPS_SSH_PORT,
    )


# ── Schemas ───────────────────────────────────────────────────────────────────

class LLMConfig(HiveBaseModel):
    provider: str  # "anthropic" | "openai" | "openrouter" | "google"
    api_key: str


class TelegramConfig(HiveBaseModel):
    bot_token: str


class AgentConfigUpdate(HiveBaseModel):
    llm: Optional[LLMConfig] = None
    telegram: Optional[TelegramConfig] = None
    restart: bool = True  # Whether to push new env vars to container immediately


class SkillAddRequest(HiveBaseModel):
    skill_id: str


# ── Config env-var mapping ────────────────────────────────────────────────────

_LLM_ENV_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "google": "GOOGLE_API_KEY",
}


def _build_container_env(config: dict) -> dict:
    """Convert stored config dict → flat env-var dict for the container."""
    env: dict[str, str] = {}

    # LLM keys
    llm = config.get("llm", {})
    for provider, key_name in _LLM_ENV_KEYS.items():
        if llm.get(provider):
            env[key_name] = llm[provider]

    # Telegram
    if config.get("telegram", {}).get("bot_token"):
        env["TELEGRAM_BOT_TOKEN"] = config["telegram"]["bot_token"]

    return env


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/{agent_id}/config")
async def get_agent_config(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Return the decrypted configuration for an owned agent."""
    agent = await _get_owned_agent(agent_id, current_user, db)
    cfg = _decrypt(agent.config_encrypted)

    # Redact secrets — return presence flags instead of raw values
    llm_providers: list[str] = list(cfg.get("llm", {}).keys())
    telegram_configured = bool(cfg.get("telegram", {}).get("bot_token"))

    skills_result = await db.execute(
        select(Skill)
        .join(AgentSkill)
        .where(AgentSkill.agent_id == agent_id)
    )
    skills = [
        {"id": s.id, "name": s.name, "display_name": s.display_name,
         "description": s.description, "tier": s.tier, "category": s.category}
        for s in skills_result.scalars()
    ]

    from services.openclaw_deployer import HIVE_DOMAIN, HIVE_URL as _HIVE_URL
    hive_url = _HIVE_URL or os.getenv("HIVE_URL", "")
    dashboard_url = None
    if agent.slug:
        if HIVE_DOMAIN:
            dashboard_url = f"https://{agent.slug}.{HIVE_DOMAIN}"
        elif hive_url:
            dashboard_url = f"{hive_url}/a/{agent.slug}/"

    return {
        "agent_id": agent_id,
        "agent_name": agent.name,
        "agent_type": agent.agent_type,
        "status": agent.status,
        "endpoint_url": agent.endpoint_url,
        "port": agent.internal_port,
        "dashboard_url": dashboard_url,
        "slug": agent.slug,
        "llm_providers_configured": llm_providers,
        "integrations": {
            "telegram": {"configured": telegram_configured},
            "gmail": {"configured": False, "available": False},
            "whatsapp": {"configured": False, "available": False},
        },
        "skills": skills,
    }


@router.put("/{agent_id}/config")
async def update_agent_config(
    agent_id: str,
    body: AgentConfigUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Update LLM keys or integration credentials for an agent.
    If restart=True (default) and the agent is an OpenClaw agent, the
    container on the VPS is reconfigured with the new env vars.
    """
    agent = await _get_owned_agent(agent_id, current_user, db)
    cfg = _decrypt(agent.config_encrypted)

    # Merge LLM key
    if body.llm:
        cfg.setdefault("llm", {})[body.llm.provider] = body.llm.api_key

    # Merge Telegram config
    if body.telegram:
        cfg.setdefault("telegram", {})["bot_token"] = body.telegram.bot_token

    agent.config_encrypted = _encrypt(cfg)
    await db.commit()

    restart_result: dict[str, Any] = {"skipped": True}
    if body.restart:
        env_vars = _build_container_env(cfg)
        restart_result = await _restart_with_config(agent, env_vars)

    return {
        "message": "Config updated",
        "restart": restart_result,
    }


@router.delete("/{agent_id}/config/llm/{provider}")
async def remove_llm_key(
    agent_id: str,
    provider: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Remove a stored LLM API key for a provider."""
    agent = await _get_owned_agent(agent_id, current_user, db)
    cfg = _decrypt(agent.config_encrypted)
    cfg.get("llm", {}).pop(provider, None)
    agent.config_encrypted = _encrypt(cfg)
    await db.commit()
    return {"message": f"{provider} key removed"}


@router.post("/{agent_id}/integrations/telegram/setup")
async def setup_telegram_webhook(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Register the Telegram webhook for this agent.
    Requires a bot_token to be stored in config first.
    Sets webhook URL to {agent_endpoint_url}/webhook/telegram.
    """
    agent = await _get_owned_agent(agent_id, current_user, db)
    cfg = _decrypt(agent.config_encrypted)
    token = cfg.get("telegram", {}).get("bot_token")
    if not token:
        raise HTTPException(status_code=400, detail="No Telegram bot token configured — save it first")

    if not agent.endpoint_url:
        raise HTTPException(status_code=400, detail="Agent has no public endpoint URL yet")

    webhook_url = f"{agent.endpoint_url}/webhook/telegram"

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{token}/setWebhook",
            json={"url": webhook_url, "drop_pending_updates": True},
        )

    data = resp.json()
    if not data.get("ok"):
        raise HTTPException(
            status_code=502,
            detail=f"Telegram API error: {data.get('description', 'unknown')}",
        )

    return {
        "message": "Telegram webhook registered",
        "webhook_url": webhook_url,
        "telegram_response": data,
    }


@router.get("/{agent_id}/integrations/telegram/info")
async def get_telegram_bot_info(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Fetch bot info from Telegram API to verify the token is valid."""
    agent = await _get_owned_agent(agent_id, current_user, db)
    cfg = _decrypt(agent.config_encrypted)
    token = cfg.get("telegram", {}).get("bot_token")
    if not token:
        raise HTTPException(status_code=400, detail="No Telegram bot token configured")

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"https://api.telegram.org/bot{token}/getMe")

    data = resp.json()
    if not data.get("ok"):
        raise HTTPException(status_code=502, detail=f"Telegram API error: {data.get('description')}")

    return {"bot": data.get("result"), "webhook_url": f"{agent.endpoint_url}/webhook/telegram"}


@router.post("/{agent_id}/skills")
async def add_skill_to_agent(
    agent_id: str,
    body: SkillAddRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Add a skill to an agent."""
    agent = await _get_owned_agent(agent_id, current_user, db)

    # Check skill exists
    skill_result = await db.execute(select(Skill).where(Skill.id == body.skill_id))
    skill = skill_result.scalar_one_or_none()
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")

    # Check not already attached
    existing = await db.execute(
        select(AgentSkill).where(
            AgentSkill.agent_id == agent_id,
            AgentSkill.skill_id == body.skill_id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Skill already attached")

    db.add(AgentSkill(agent_id=agent_id, skill_id=body.skill_id, config={}))
    await db.commit()
    return {"message": f"Skill '{skill.display_name}' added"}


@router.delete("/{agent_id}/skills/{skill_id}")
async def remove_skill_from_agent(
    agent_id: str,
    skill_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Remove a skill from an agent."""
    agent = await _get_owned_agent(agent_id, current_user, db)

    result = await db.execute(
        select(AgentSkill).where(
            AgentSkill.agent_id == agent_id,
            AgentSkill.skill_id == skill_id,
        )
    )
    agent_skill = result.scalar_one_or_none()
    if not agent_skill:
        raise HTTPException(status_code=404, detail="Skill not attached to this agent")

    await db.delete(agent_skill)
    await db.commit()
    return {"message": "Skill removed"}
