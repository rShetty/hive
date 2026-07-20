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

    env = {
        **os.environ,
        # User-supplied keys first (so per-user providers/models win)...
        **(env_vars or {}),
        # ...then any server-level fallbacks.
        **llm_env,
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
