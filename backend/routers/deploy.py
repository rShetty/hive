"""Agent deployment routes for users."""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from schemas import HiveBaseModel

from database import get_db
from models.agent import Agent, AgentStatus
from models.skill import Skill
from models.agent_skill import AgentSkill
from models.user import User
from schemas import AgentResponse, AgentCreate, HostedAgentRequest, HostedAgentResponse
from auth import get_current_active_user
from services.container_manager import create_container, delete_container, get_container_logs
from services.health_checker import perform_endpoint_challenge
from services.skill_catalog import validate_skill_selection, get_skill_by_name
from cryptography.fernet import Fernet
import os
import json

router = APIRouter(prefix="/api", tags=["deploy"])


def _normalize_model_key(model_key) -> dict:
    """Normalise an LLM model_key into a flat {provider: key} map.

    Accepts both shapes the frontend has sent historically:
      - flat:  {"openrouter": "sk-..."}
      - nested: {"provider": "openrouter", "key": "sk-..."}
    Unknown shapes are passed through untouched.
    """
    if not model_key or not isinstance(model_key, dict):
        return {}
    if "provider" in model_key and "key" in model_key:
        return {str(model_key["provider"]): model_key["key"]}
    return {str(k): v for k, v in model_key.items()}


def _mcp_headers_for(server) -> dict:
    """Build the auth headers an agent should send to an MCP server.

    Starts from any statically configured headers, then (for OAuth servers
    with a stored access token) adds a Bearer Authorization header.
    """
    from services.crypto import decrypt_json
    headers = dict(decrypt_json(getattr(server, "headers_encrypted", None)) or {})
    if getattr(server, "auth_type", "headers") == "oauth":
        blob = decrypt_json(getattr(server, "oauth_encrypted", None)) or {}
        token = blob.get("access_token")
        if token:
            headers["Authorization"] = f"{blob.get('token_type', 'Bearer')} {token}"
    return headers

# Encryption key for API keys — MUST be set in production.
# Generating a fallback for local dev only; data encrypted with this key
# becomes unreadable if the process restarts.
_env_key = os.getenv("ENCRYPTION_KEY")
if not _env_key:
    import warnings
    warnings.warn(
        "ENCRYPTION_KEY not set — generating ephemeral key. "
        "Encrypted data will be lost on restart. Set ENCRYPTION_KEY in production.",
        stacklevel=2,
    )
    ENCRYPTION_KEY = Fernet.generate_key()
else:
    # Fernet requires url-safe base64 key; accept raw or already-encoded
    import base64
    try:
        ENCRYPTION_KEY = _env_key.encode() if isinstance(_env_key, str) else _env_key
        Fernet(ENCRYPTION_KEY)  # validate
    except Exception:
        ENCRYPTION_KEY = base64.urlsafe_b64encode(_env_key.encode().ljust(32, b'\0')[:32])
fernet = Fernet(ENCRYPTION_KEY)


def decrypt_api_keys(encrypted_data: str) -> dict:
    """Decrypt user's model API keys."""
    if not encrypted_data:
        return {}
    try:
        decrypted = fernet.decrypt(encrypted_data.encode())
        return json.loads(decrypted)
    except Exception:
        return {}


@router.post("/agents/deploy", response_model=AgentResponse)
async def deploy_agent(
    agent_data: AgentCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Deploy a new agent with selected skills.
    User's model API keys are injected into the container.
    """
    # Validate skill selection
    user_api_keys = decrypt_api_keys(current_user.model_api_keys_encrypted or "")
    
    is_valid, error_msg = await validate_skill_selection(
        db, agent_data.skill_ids, user_api_keys
    )
    
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_msg
        )
    
    # Get skill details for container
    skills = []
    for skill_id in agent_data.skill_ids:
        result = await db.execute(select(Skill).where(Skill.id == skill_id))
        skill = result.scalar_one_or_none()
        if skill:
            skills.append({
                "id": skill.id,
                "name": skill.name,
                "tier": skill.tier
            })
    
    # Create agent record first (to get ID)
    import secrets
    from auth import get_password_hash
    
    api_key = f"am-{secrets.token_urlsafe(32)}"
    api_key_hash = get_password_hash(api_key)

    slug = agent_data.slug or Agent.generate_slug(agent_data.name)

    agent = Agent(
        name=agent_data.name,
        description=agent_data.description,
        slug=slug,
        avatar_url=agent_data.avatar_url,
        capabilities=agent_data.capabilities or [],
        tags=agent_data.tags or [],
        owner_id=current_user.id,
        api_key_prefix=api_key[:16],
        api_key_hash=api_key_hash,
        status=AgentStatus.PENDING.value,
        version="1.0.0"
    )
    
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    
    try:
        # Create container
        container_id, port = create_container(
            agent_id=agent.id,
            agent_name=agent.name,
            skills=skills,
            env_vars=user_api_keys,
            api_key=api_key
        )
        
        # Update agent with container info
        agent.container_id = container_id
        agent.internal_port = port
        agent.endpoint_url = f"/agents/{agent.id}/invoke"
        agent.status = AgentStatus.VERIFYING.value
        await db.commit()
        
        # Add skills to agent
        for skill_id in agent_data.skill_ids:
            config = agent_data.skill_configs.get(skill_id, {}) if agent_data.skill_configs else {}
            agent_skill = AgentSkill(
                agent_id=agent.id,
                skill_id=skill_id,
                config=config
            )
            db.add(agent_skill)
        
        await db.commit()
        
        # Trigger endpoint challenge in a background task with its own session
        import asyncio
        asyncio.create_task(_run_endpoint_challenge(agent.id))
        
        return agent
    
    except Exception as e:
        # Cleanup on failure
        import logging
        logging.getLogger(__name__).error("Failed to deploy agent %s: %s", agent.id, e)
        agent.status = AgentStatus.ERROR.value
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to deploy agent. Check server logs for details."
        )


async def _run_endpoint_challenge(agent_id: str):
    """Run endpoint challenge with an independent DB session."""
    from database import async_session_maker
    async with async_session_maker() as session:
        await perform_endpoint_challenge(session, agent_id)


@router.delete("/agents/{agent_id}")
async def delete_agent(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Delete an agent (owner only)."""
    result = await db.execute(
        select(Agent).where(Agent.id == agent_id)
    )
    agent = result.scalar_one_or_none()
    
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found"
        )
    
    # Check ownership
    if agent.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to delete this agent"
        )
    
    # Stop and delete container
    if agent.container_id:
        delete_container(agent.container_id)
    
    # Delete from database
    await db.delete(agent)
    await db.commit()
    
    return {"message": "Agent deleted successfully"}


@router.post("/agents/{agent_id}/restart")
async def restart_agent(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Restart an agent container (owner only)."""
    result = await db.execute(
        select(Agent).where(Agent.id == agent_id)
    )
    agent = result.scalar_one_or_none()
    
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found"
        )
    
    if agent.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized"
        )
    
    if agent.container_id:
        from services.container_manager import start_container
        success = start_container(agent.container_id)
        if success:
            agent.status = AgentStatus.PENDING.value
            await db.commit()
            
            # Trigger new challenge
            import asyncio
            asyncio.create_task(_run_endpoint_challenge(agent.id))
            
            return {"message": "Agent restart initiated"}
    
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Failed to restart agent"
    )


@router.get("/agents/{agent_id}/logs")
async def get_agent_logs(
    agent_id: str,
    tail: int = 100,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get agent container logs (owner only)."""
    result = await db.execute(
        select(Agent).where(Agent.id == agent_id)
    )
    agent = result.scalar_one_or_none()
    
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found"
        )
    
    if agent.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized"
        )
    
    if agent.container_id:
        logs = get_container_logs(agent.container_id, tail)
        return {"logs": logs}
    
    return {"logs": "No container found"}


# ============ OpenClaw VPS Deployment ============

# Standard skills automatically attached to every OpenClaw agent.
# Users can add more during setup.
OPENCLAW_DEFAULT_SKILL_NAMES = [
    "terminal",
    "file_ops",
    "web_extract",
    "planning",
    "code_review",
]


async def _resolve_skill_names(
    db: AsyncSession, names: list[str]
) -> list[Skill]:
    """Resolve a list of skill machine-names to Skill rows."""
    from services.skill_catalog import get_skill_by_name
    resolved = []
    for name in names:
        skill = await get_skill_by_name(db, name)
        if skill:
            resolved.append(skill)
    return resolved


OPENCLAW_VPS_HOST = os.getenv("OPENCLAW_VPS_HOST")
OPENCLAW_VPS_SSH_KEY_PATH = os.getenv("OPENCLAW_VPS_SSH_KEY_PATH")
OPENCLAW_VPS_SSH_USER = os.getenv("OPENCLAW_VPS_SSH_USER", "root")
OPENCLAW_VPS_SSH_PORT = int(os.getenv("OPENCLAW_VPS_SSH_PORT", "22"))
OPENCLAW_PORT_START = int(os.getenv("OPENCLAW_PORT_START", "9000"))
HIVE_URL = os.getenv("HIVE_URL", "http://localhost:8080")


def _port_is_free(port: int) -> bool:
    """True if nothing is currently listening on 127.0.0.1:<port>."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", port)) != 0


async def _get_next_available_port(db: AsyncSession) -> int:
    """Get the next port that is both unused in the DB and free on the host.

    Considers every agent with an assigned ``internal_port`` (openclaw AND
    managed/hosted), then probes the host to ensure the port is not already
    bound by a live runtime. This prevents two agents colliding on the same
    port when an earlier agent's process died without freeing its port.
    """
    result = await db.execute(
        select(Agent.internal_port)
        .where(Agent.internal_port.isnot(None))
    )
    used = {row[0] for row in result.fetchall() if row[0] is not None}

    candidate = OPENCLAW_PORT_START
    while candidate < OPENCLAW_PORT_START + 2000:
        if candidate in used or not _port_is_free(candidate):
            candidate += 1
            continue
        return candidate
    raise RuntimeError("No available ports in range for agent deployment")


class OpenClawDeployRequest(HiveBaseModel):
    """Request body for one-click OpenClaw deployment."""
    agent_name: str
    extra_env: Optional[dict] = None
    tags: List[str] = []
    extra_skill_names: List[str] = []


@router.post("/agents/deploy-openclaw")
async def deploy_openclaw_agent(
    req: OpenClawDeployRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    One-click deploy: create an OpenClaw agent on a shared VPS via Docker Compose
    over SSH. VPS config is read from environment variables.
    """
    from models.agent import AgentType
    from services.openclaw_deployer import generate_compose, deploy_to_vps
    import secrets as _secrets
    import uuid as _uuid
    from auth import get_password_hash as _hash

    if not OPENCLAW_VPS_HOST:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="OPENCLAW_VPS_HOST not configured on server.",
        )
    if not OPENCLAW_VPS_SSH_KEY_PATH:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="OPENCLAW_VPS_SSH_KEY_PATH not configured on server.",
        )
    import os as _os
    if not _os.path.isfile(OPENCLAW_VPS_SSH_KEY_PATH):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"SSH key not found at {OPENCLAW_VPS_SSH_KEY_PATH}. "
                   "Mount the key into the container or set the correct path.",
        )

    instance_id = str(_uuid.uuid4())
    api_key = f"am-{_secrets.token_urlsafe(32)}"
    slug = Agent.generate_slug(req.agent_name)
    port = await _get_next_available_port(db)

    all_skill_names = list(dict.fromkeys(
        OPENCLAW_DEFAULT_SKILL_NAMES + req.extra_skill_names
    ))
    resolved_skills = await _resolve_skill_names(db, None, all_skill_names)

    agent = Agent(
        name=req.agent_name,
        description=f"OpenClaw instance on {OPENCLAW_VPS_HOST}:{port}",
        slug=slug,
        agent_type=AgentType.OPENCLAW.value,
        capabilities=["openclaw", "vps-deploy"],
        tags=req.tags or ["openclaw"],
        owner_id=current_user.id,
        api_key_prefix=api_key[:16],
        api_key_hash=_hash(api_key),
        status=AgentStatus.PENDING.value,
        version="1.0.0",
        internal_port=port,
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)

    for skill in resolved_skills:
        db.add(AgentSkill(agent_id=agent.id, skill_id=skill.id, config={}))
    await db.commit()

    # -----------------------------------------------------------------
    # Decide deployment mode:
    #   OPENCLAW_DEPLOY_MODE=local  → always local Docker daemon
    #   OPENCLAW_DEPLOY_MODE=vps     → always remote VPS (requires VPS env)
    #   OPENCLAW_DEPLOY_MODE=auto (default) → VPS if configured, else local
    # -----------------------------------------------------------------
    deploy_mode = os.getenv("OPENCLAW_DEPLOY_MODE", "auto").lower()
    vps_configured = bool(OPENCLAW_VPS_HOST and OPENCLAW_VPS_SSH_KEY_PATH)
    if deploy_mode == "local":
        use_remote_vps = False
    elif deploy_mode == "vps":
        use_remote_vps = True
    else:  # auto
        use_remote_vps = vps_configured

    if use_remote_vps:
        compose = generate_compose(
            instance_id=instance_id,
            agent_name=req.agent_name,
            agent_id=agent.id,
            api_key=api_key,
            port=port,
            extra_env=req.extra_env,
        )

        result = await deploy_to_vps(
            vps_host=OPENCLAW_VPS_HOST,
            ssh_key_path=OPENCLAW_VPS_SSH_KEY_PATH,
            compose_content=compose,
            instance_id=instance_id,
            port=port,
            ssh_user=OPENCLAW_VPS_SSH_USER,
            ssh_port=OPENCLAW_VPS_SSH_PORT,
            agent_slug=slug,
            extra_env=req.extra_env,
            config_env=None,
        )
    else:
        # Local single-host deploy — uses the Docker socket already mounted
        from services.container_manager import create_openclaw_container
        try:
            env_vars = dict(req.extra_env or {})
            # Forward the resolved skill names so the running agent knows its skills.
            env_vars["SKILLS"] = ",".join([s.name for s in resolved_skills])

            # Inject the owner's saved model API keys so the agent can make
            # real LLM calls. Map provider names → the env vars the OpenClaw
            # agent honours (OPENROUTER_API_KEY, OPENAI_API_KEY, ...).
            _KEY_ENV_MAP = {
                "openrouter": "OPENROUTER_API_KEY",
                "openai": "OPENAI_API_KEY",
                "anthropic": "ANTHROPIC_API_KEY",
                "google": "GOOGLE_API_KEY",
                "cohere": "COHERE_API_KEY",
            }
            _user_keys = decrypt_api_keys(current_user.model_api_keys_encrypted or "")
            for _prov, _val in _user_keys.items():
                _env = _KEY_ENV_MAP.get(_prov.lower())
                if _env and _val:
                    env_vars[_env] = _val
            # Let an explicit server-side model selection win when the user
            # has not pinned a model themselves.
            if os.getenv("OPENROUTER_MODEL") and _user_keys.get("openrouter"):
                env_vars.setdefault("OPENROUTER_MODEL", os.getenv("OPENROUTER_MODEL"))
            container_id, assigned_port = create_openclaw_container(
                agent_id=agent.id,
                agent_name=req.agent_name,
                env_vars=env_vars,
                api_key=api_key,
                slug=slug,
                hive_domain=os.getenv("HIVE_DOMAIN", ""),
            )
        except Exception as e:
            result = {"success": False, "message": str(e)}
        else:
            agent.container_id = container_id
            agent.internal_port = assigned_port
            # Build URL: prefer subdomain if domain is set, else direct IP:port
            hive_domain = os.getenv("HIVE_DOMAIN", "")
            if hive_domain:
                dashboard_url = f"https://{slug}.{hive_domain}"
            else:
                # Fallback to host IP + assigned port
                host_ip = os.getenv("HOST_IP", "127.0.0.1")
                dashboard_url = f"http://{host_ip}:{assigned_port}"

            result = {
                "success": True,
                "message": "OpenClaw deployed locally via Docker daemon",
                "url": dashboard_url,
                "dashboard_url": dashboard_url,
                "remote_dir": None,
            }

    if result["success"]:
        agent.status = AgentStatus.ACTIVE.value
        agent.endpoint_url = result.get("url", "")
        agent.openclaw_instance_id = instance_id
        # Persist the raw API key encrypted so config updates can restart with it
        import json as _json
        agent.config_encrypted = fernet.encrypt(
            _json.dumps({"_hive_api_key": api_key}).encode()
        ).decode()
        await db.commit()
        
        registration_prompt = f"""You are {req.agent_name}, an OpenClaw agent registered with Hive.

Your agent ID: {agent.id}
Your API key: {api_key}
Hive endpoint: {HIVE_URL}
Your public URL: {result["url"]}

You are already registered. Send heartbeats every 60 seconds to stay active:
curl -X POST {HIVE_URL}/api/agent/heartbeat \\
  -H "X-API-Key: {api_key}"

Report task completion:
curl -X POST {HIVE_URL}/api/delegate/{{delegation_id}}/complete \\
  -H "X-API-Key: {api_key}" \\
  -H "Content-Type: application/json" \\
  -d '{{"tokens_used": 1.0, "result": {{"output": "Task completed successfully"}}}}'

Your capabilities: {', '.join([s.name for s in resolved_skills])}"""
        
        dashboard_url = result.get("dashboard_url") or result["url"]

        return {
            "agent_id": agent.id,
            "slug": agent.slug,
            "agent_type": agent.agent_type,
            "status": agent.status,
            "url": result["url"],
            "dashboard_url": dashboard_url,
            "port": port,
            "api_key": api_key,
            "skills": [s.name for s in resolved_skills],
            "message": result["message"],
            "registration_prompt": registration_prompt,
            "hive_url": HIVE_URL,
        }
    else:
        agent.status = AgentStatus.ERROR.value
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=result["message"],
        )


async def _resolve_skill_names(db, skill_ids, skill_names):
    """Return Skill rows resolved by id or by machine name."""
    from models.skill import Skill
    names = []
    if skill_ids:
        res = await db.execute(select(Skill).where(Skill.id.in_(skill_ids)))
        names += list(res.scalars().all())
    for nm in (skill_names or []):
        skill = await get_skill_by_name(db, nm)
        if skill and skill.id not in {s.id for s in names}:
            names.append(skill)
    return names


@router.post("/agents/deploy-hosted", response_model=HostedAgentResponse)
async def deploy_hosted_agent(
    req: HostedAgentRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Bring-Your-Own-Key hosted agent.

    The platform hosts the agent runtime — no endpoint_url is required. The
    user supplies an LLM key (+ optional MCP servers) and picks the tools
    (skills) the agent should have. Hive spins up a running OpenClaw agent
    that accepts requests at the platform-assigned endpoint and exposes a
    chat UI + dashboard at /a/{slug}/.
    """
    from models.agent import AgentType
    from auth import get_password_hash as _hash
    import secrets as _secrets
    import uuid as _uuid

    if not req.name or not req.name.strip():
        raise HTTPException(status_code=400, detail="Agent name is required")

    # Need at least one tool (skill) so the agent can do real work.
    resolved_skills = await _resolve_skill_names(db, req.skill_ids, req.skill_names)
    if not resolved_skills:
        raise HTTPException(
            status_code=400,
            detail="Select at least one tool (skill) for your agent.",
        )

    api_key = f"am-{_secrets.token_urlsafe(32)}"
    api_key_hash = _hash(api_key)
    slug = Agent.generate_slug(req.name)
    port = await _get_next_available_port(db)

    agent = Agent(
        name=req.name,
        description=req.description or f"Hosted {req.framework} agent on Hive.",
        slug=slug,
        agent_type=AgentType.MANAGED.value,
        capabilities=(req.capabilities or []) + [req.framework],
        tags=req.tags or ["hosted", req.framework],
        owner_id=current_user.id,
        api_key_prefix=api_key[:16],
        api_key_hash=api_key_hash,
        status=AgentStatus.VERIFYING.value,
        version="1.0.0",
        internal_port=port,
        endpoint_url=f"/agents/{'PLACEHOLDER'}",  # fixed after we know the id
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)

    agent.endpoint_url = f"/agents/{agent.id}/invoke"
    await db.commit()

    # Persist config (LLM key + MCP servers + framework) encrypted.
    mcp_list = [s.model_dump() for s in req.mcp_servers]
    flat_model_key = _normalize_model_key(req.model_key)
    agent.config_encrypted = fernet.encrypt(json.dumps({
        "framework": req.framework,
        "model_key": flat_model_key,
        "mcp_servers": mcp_list,
    }).encode()).decode()

    # Attach selected tools.
    for skill in resolved_skills:
        db.add(AgentSkill(agent_id=agent.id, skill_id=skill.id, config={}))
    await db.commit()

    # ---- Resolve MCP servers (ad-hoc specs + user registry references) ----
    from models.mcp import MCPServer, AgentMCPAccess
    from services.crypto import decrypt_json
    mcp_final = list(mcp_list)  # ad-hoc specs already collected
    granted_server_ids = []
    for sid in (req.mcp_server_ids or []):
        srow = await db.execute(
            select(MCPServer).where(
                MCPServer.id == sid, MCPServer.owner_id == current_user.id
            )
        )
        srv = srow.scalar_one_or_none()
        if not srv:
            continue  # skip servers the user doesn't own
        # Create an explicit per-agent access grant.
        grant = AgentMCPAccess(
            agent_id=agent.id, mcp_server_id=srv.id, enabled=True,
            headers_encrypted=srv.headers_encrypted,
        )
        db.add(grant)
        granted_server_ids.append(srv.id)
        mcp_final.append({
            "name": srv.name,
            "url": srv.url,
            "description": srv.description,
            "transport": srv.transport,
            "headers": _mcp_headers_for(srv),
            "command": srv.command,
            "env": decrypt_json(srv.env_encrypted) or {},
        })
    await db.commit()

    # Build the runtime env. The user's LLM key (if any) wins; otherwise the
    # server-level key is used as a fallback by the runtime.
    env_vars = {
        "SKILLS": ",".join([s.name for s in resolved_skills]),
    }
    _KEY_ENV_MAP = {
        "openrouter": "OPENROUTER_API_KEY",
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "google": "GOOGLE_API_KEY",
        "cohere": "COHERE_API_KEY",
    }
    for _prov, _val in _normalize_model_key(req.model_key).items():
        _env = _KEY_ENV_MAP.get(_prov.lower())
        if _env and _val:
            env_vars[_env] = _val
    if mcp_final:
        env_vars["MCP_SERVERS"] = json.dumps(mcp_final)

    container_id = None
    try:
        from services.openclaw_local import spawn_openclaw_agent
        container_id = spawn_openclaw_agent(
            agent_id=agent.id,
            agent_name=req.name,
            port=port,
            api_key=api_key,
            skills=[s.name for s in resolved_skills],
            env_vars=env_vars,
        )
        agent.container_id = container_id
        agent.status = AgentStatus.ACTIVE.value
        await db.commit()
        # Start the endpoint challenge so the agent is reachable via the proxy.
        import asyncio
        asyncio.create_task(_run_endpoint_challenge(agent.id))
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("Hosted agent spawn failed for %s: %s", agent.id, e)
        agent.status = AgentStatus.ERROR.value
        agent.container_id = None
        await db.commit()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to start agent runtime: {e}",
        )

    dashboard_url = f"/a/{slug}/"
    return {
        "agent_id": agent.id,
        "slug": slug,
        "api_key": api_key,
        "url": dashboard_url,
        "dashboard_url": dashboard_url,
        "endpoint_url": agent.endpoint_url,
        "status": agent.status,
    }


@router.patch("/me/keys")
async def update_model_api_keys(
    keys: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Update user's model API keys (encrypted)."""
    # Validate keys format
    allowed_providers = [
        "openai", "anthropic", "openrouter", "google", "cohere",
    ]
    filtered_keys = {k: v for k, v in keys.items() if k in allowed_providers and isinstance(v, str) and v.strip()}
    
    # Encrypt and store
    encrypted = fernet.encrypt(json.dumps(filtered_keys).encode())
    current_user.model_api_keys_encrypted = encrypted.decode()
    
    await db.commit()
    
    return {"message": "API keys updated", "providers": list(filtered_keys.keys())}
