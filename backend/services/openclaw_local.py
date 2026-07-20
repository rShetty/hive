"""Run OpenClaw agents as local OS processes (no Docker required).

When Docker is unavailable (e.g. a local dev sandbox), Hive can still deploy a
*real*, running OpenClaw agent by launching ``docker/agent_app/main.py`` as a
subprocess bound to ``127.0.0.1:<port>``. This gives genuine end-to-end behaviour
— the agent serves its dashboard, answers heartbeats, and processes delegations
via its ``/delegate`` endpoint, streaming progress back to Hive.

Each spawned process is tracked so it can be stopped/cleaned up on agent delete
or server shutdown.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
from typing import Optional

# Map: agent_id -> subprocess.Popen (or None once reaped)
_RUNNERS: dict[str, "subprocess.Popen | None"] = {}
_RUNNERS_LOCK = threading.Lock()

# Path to the OpenClaw agent application shipped with Hive.
_AGENT_APP_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "docker", "agent_app")
)
_AGENT_MAIN = os.path.join(_AGENT_APP_DIR, "main.py")

# Python interpreter used to launch the agent. Defaults to sys.executable so we
# reuse the Hive backend virtualenv (it already has fastapi/uvicorn/httpx).
import sys
_PYTHON = os.getenv("OPENCLAW_PYTHON", sys.executable)


def _find_python() -> str:
    """Return a usable python interpreter (prefer the one running Hive)."""
    if shutil.which(_PYTHON):
        return _PYTHON
    return sys.executable


def spawn_openclaw_agent(
    agent_id: str,
    agent_name: str,
    port: int,
    api_key: str,
    skills: list[str] | None = None,
    hive_url: str | None = None,
    env_vars: dict[str, str] | None = None,
) -> str:
    """Launch a real OpenClaw agent process on 127.0.0.1:<port>.

    Returns a synthetic "container id" string (``proc-openclaw-<short>``) so the
    rest of the codebase can treat it like any other container handle.
    """
    if not os.path.isfile(_AGENT_MAIN):
        raise RuntimeError(f"OpenClaw agent app not found at {_AGENT_MAIN}")

    # Locally-spawned agents MUST reach the SAME local Hive instance, not the
    # public MARKETPLACE_URL (which may point at a different/prod host). Force
    # the agent's Hive endpoint to the local server so heartbeats, progress,
    # and completion callbacks land on the instance that deployed it.
    # Derive the URL from HIVE_URL but point it at localhost (preserving the
    # configured port) so the agent always talks to its own Hive, whether that
    # is :8000 in dev or :8080 on a VPS.
    if not hive_url:
        _configured = os.getenv("HIVE_URL") or os.getenv("OPENCLAW_LOCAL_HIVE_URL")
        if _configured:
            from urllib.parse import urlparse
            _p = urlparse(_configured)
            _port = f":{_p.port}" if _p.port else ""
            hive_url = f"http://localhost{_port}"
        else:
            hive_url = "http://localhost:8000"

    # Forward any user-provided LLM credentials so the agent can make real
    # model calls. OPENROUTER_API_KEY (+ optional OPENROUTER_MODEL) is the
    # primary path; the agent app also honours ANTHROPIC/OPENAI/GOOGLE keys.
    # User keys (saved in Settings and decrypted by the deploy endpoint) arrive
    # via ``env_vars``; we fall back to server-level keys if none were supplied.
    llm_env = {}
    openrouter_key = os.getenv("OPENROUTER_API_KEY")
    if openrouter_key and not (env_vars or {}).get("OPENROUTER_API_KEY"):
        llm_env["OPENROUTER_API_KEY"] = openrouter_key
        llm_env["OPENROUTER_MODEL"] = os.getenv("OPENROUTER_MODEL", "google/gemma-4-26b-a4b-it")

    # Secret values (API keys/tokens) are written to files and exposed via
    # ``<NAME>_FILE`` instead of plaintext env, so they don't leak into the
    # process environment / ps output.
    from services.secrets import split_secrets
    plain_env, secret_values = split_secrets({**(env_vars or {}), **llm_env})

    secret_file_env = {}
    secret_dir = os.path.join("/tmp", "hive-secrets", f"proc-{agent_id[:8]}")
    os.makedirs(secret_dir, exist_ok=True)
    for name, value in secret_values.items():
        secret_path = os.path.join(secret_dir, name.lower())
        with open(secret_path, "w") as fh:
            fh.write(value)
        os.chmod(secret_path, 0o600)
        secret_file_env[f"{name}_FILE"] = secret_path

    env = {
        **os.environ,
        # Plain user-supplied env (skills, model names, urls, etc.)...
        **plain_env,
        # ...secret values surfaced via *_FILE...
        **secret_file_env,
        "AGENT_ID": agent_id,
        "AGENT_NAME": agent_name,
        "AGENT_API_KEY": api_key,
        "HIVE_URL": hive_url,
        "HIVE_API_KEY": api_key,
        "INSTANCE_ID": agent_id,
        "SKILLS": ",".join(skills or []),
        "PORT": str(port),
    }

    python = _find_python()
    log_path = os.path.join(_AGENT_APP_DIR, f"openclaw-{agent_id[:8]}.log")

    proc = subprocess.Popen(
        [python, "-m", "uvicorn", "main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=_AGENT_APP_DIR,
        env=env,
        stdout=open(log_path, "a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    with _RUNNERS_LOCK:
        _RUNNERS[agent_id] = proc

    return f"proc-openclaw-{agent_id[:8]}"


async def rehydrate_local_agents(db) -> int:
    """Re-spawn managed agents that were deployed as local subprocesses.

    These agents live as children of the Hive process, so they die whenever
    Hive restarts. On startup we bring them back using their persisted
    (encrypted) config + skill list, generating a fresh API key.

    Returns the number of agents rehydrated.
    """
    from sqlalchemy import select
    from models.agent import Agent, AgentStatus
    from models.agent_skill import AgentSkill
    from models.skill import Skill
    from services.crypto import decrypt_json
    from auth import get_password_hash
    import secrets as _secrets

    _KEY_ENV_MAP = {
        "openrouter": "OPENROUTER_API_KEY",
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "google": "GOOGLE_API_KEY",
        "cohere": "COHERE_API_KEY",
    }

    def _flat_keys(model_key) -> dict:
        if not isinstance(model_key, dict):
            return {}
        if "provider" in model_key and "key" in model_key:
            return {str(model_key["provider"]): model_key["key"]}
        return {str(k): v for k, v in model_key.items()}

    result = await db.execute(
        select(Agent).where(Agent.container_id.like("proc-openclaw-%"))
    )
    agents = result.scalars().all()
    count = 0
    for agent in agents:
        # Skip agents already running (a live process exists).
        with _RUNNERS_LOCK:
            existing = _RUNNERS.get(agent.id)
        if existing is not None and existing.poll() is None:
            continue

        cfg = decrypt_json(agent.config_encrypted) or {}
        model_key = _flat_keys(cfg.get("model_key"))
        # Resolve skill names from the join table.
        skill_rows = (await db.execute(
            select(Skill.name).join(
                AgentSkill, AgentSkill.skill_id == Skill.id
            ).where(AgentSkill.agent_id == agent.id)
        )).scalars().all()
        skills = list(skill_rows) or [s.get("name") for s in (cfg.get("mcp_servers") or [])]

        env_vars = {"SKILLS": ",".join([s for s in skills if s])}
        for prov, val in model_key.items():
            env = _KEY_ENV_MAP.get(str(prov).lower())
            if env and val:
                env_vars[env] = str(val)

        # Fresh API key (plaintext is not persisted; only the hash).
        api_key = f"am-{_secrets.token_urlsafe(32)}"
        agent.api_key_hash = get_password_hash(api_key)
        agent.api_key_prefix = api_key[:16]

        port = agent.internal_port or 0
        try:
            container_id = spawn_openclaw_agent(
                agent_id=agent.id,
                agent_name=agent.name,
                port=port,
                api_key=api_key,
                skills=[s for s in skills if s],
                env_vars=env_vars,
            )
        except Exception as e:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).error(
                "Rehydrate failed for agent %s: %s", agent.id, e
            )
            agent.status = AgentStatus.ERROR.value
            await db.commit()
            continue

        agent.container_id = container_id
        agent.status = AgentStatus.ACTIVE.value
        await db.commit()
        count += 1
        import logging
        logging.getLogger(__name__).info("Rehydrated local agent %s", agent.id)
    return count


def stop_openclaw_agent(agent_id: str) -> bool:
    """Terminate the locally-running OpenClaw agent for ``agent_id``."""
    with _RUNNERS_LOCK:
        proc = _RUNNERS.pop(agent_id, None)
    if proc is None:
        return True
    if proc.poll() is not None:
        return True
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        return True
    except Exception:
        try:
            proc.terminate()
        except Exception:
            return False
    return True


def cleanup_all() -> None:
    """Stop every locally-spawned OpenClaw agent (used on shutdown)."""
    with _RUNNERS_LOCK:
        ids = list(_RUNNERS.keys())
    for aid in ids:
        stop_openclaw_agent(aid)
