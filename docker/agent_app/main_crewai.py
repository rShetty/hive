"""Hive CrewAI agent — HTTP API + web dashboard.

Uses CrewAI for multi-agent orchestration with tool support.
Same interface as the OpenClaw agent (main.py).
"""
import asyncio
import os
import json
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
import httpx

from mcp_client import MCPManager

app = FastAPI(title="CrewAI Agent")

AGENT_ID = os.getenv("AGENT_ID", "unknown")
AGENT_NAME = os.getenv("AGENT_NAME", "Unknown Agent")
SKILLS = [s for s in os.getenv("SKILLS", "").split(",") if s]
HIVE_URL = os.getenv("HIVE_URL", "")
HIVE_API_KEY = os.getenv("HIVE_API_KEY", "")
INSTANCE_ID = os.getenv("INSTANCE_ID", "")

try:
    MCP_SERVERS = json.loads(os.getenv("MCP_SERVERS", "[]") or "[]")
    if not isinstance(MCP_SERVERS, list):
        MCP_SERVERS = []
except Exception:
    MCP_SERVERS = []

_activity: list[dict] = []
_start_time = datetime.utcnow()
MCP_MANAGER: Optional[MCPManager] = None


def _build_mcp():
    global MCP_MANAGER
    if not MCP_SERVERS:
        return
    MCP_MANAGER = MCPManager(MCP_SERVERS)


def _log_activity(kind: str, summary: str, detail: Any = None):
    _activity.insert(0, {
        "kind": kind,
        "summary": summary,
        "detail": detail,
        "ts": datetime.utcnow().isoformat(),
    })
    if len(_activity) > 50:
        _activity.pop()


# ── LLM integration ────────────────────────────────────────────────────────

def _resolve_llm():
    """Return (base_url, api_key, model) for the first configured provider."""
    def _secret(provider_env: str) -> Optional[str]:
        file_env = provider_env + "_FILE"
        path = os.getenv(file_env)
        if path and os.path.isfile(path):
            try:
                with open(path, "r") as fh:
                    return fh.read().strip()
            except OSError:
                return None
        return os.getenv(provider_env)

    if _secret("OPENROUTER_API_KEY"):
        return {
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": _secret("OPENROUTER_API_KEY"),
            "model": os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini"),
        }
    if _secret("OPENAI_API_KEY"):
        return {
            "base_url": "https://api.openai.com/v1",
            "api_key": _secret("OPENAI_API_KEY"),
            "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        }
    if _secret("ANTHROPIC_API_KEY"):
        return {
            "base_url": "https://api.anthropic.com/v1",
            "api_key": _secret("ANTHROPIC_API_KEY"),
            "model": os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest"),
        }
    if _secret("GOOGLE_API_KEY"):
        return {
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "api_key": _secret("GOOGLE_API_KEY"),
            "model": os.getenv("GOOGLE_MODEL", "gemini-1.5-flash"),
        }
    return None


def _get_crewai_llm():
    """Get a CrewAI-compatible LLM from the configured provider."""
    try:
        from langchain_openai import ChatOpenAI
    except ImportError:
        raise RuntimeError("langchain-openai is not installed (required by CrewAI)")

    cfg = _resolve_llm()
    if not cfg:
        return None

    return ChatOpenAI(
        model=cfg["model"],
        api_key=cfg["api_key"],
        base_url=cfg["base_url"],
        temperature=0,
        max_tokens=1024,
    )


def _get_crewai_tools():
    """Convert MCP tools to CrewAI tools."""
    try:
        from crewai.tools import BaseTool
    except ImportError:
        raise RuntimeError("crewai is not installed")

    if not MCP_MANAGER:
        return []

    tools = []
    for qualified_name, (_conn, t) in MCP_MANAGER.tool_index.items():
        tool_desc = t.get("description", "") or f"Tool {t.get('name')}"

        class MCPCrewTool(BaseTool):
            name: str = qualified_name
            description: str = tool_desc

            def _run(self, arguments: str = "{}") -> str:
                try:
                    args = json.loads(arguments) if isinstance(arguments, str) else arguments
                except Exception:
                    args = {}
                # CrewAI tools run synchronously, but MCP calls are async
                # Use asyncio.run_coroutine_threadsafe if needed
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        future = pool.submit(asyncio.run, MCP_MANAGER.call(qualified_name, args))
                        return future.result(timeout=30)
                else:
                    return loop.run_until_complete(MCP_MANAGER.call(qualified_name, args))

        tools.append(MCPCrewTool())

    return tools


async def _call_llm(task: str, system: str = "") -> str:
    """Call the configured LLM via CrewAI, executing any MCP tools."""
    try:
        llm = _get_crewai_llm()
    except RuntimeError as e:
        return f"[{AGENT_NAME}] Task received: {task[:200]}. {e}"

    if not llm:
        return (
            f"[{AGENT_NAME}] Task received: {task[:200]}. "
            "Connect an LLM via Hive to enable real task execution."
        )

    try:
        from crewai import Agent, Task, Crew

        tools = _get_crewai_tools()

        agent = Agent(
            role=f"{AGENT_NAME} Assistant",
            goal=f"Complete the given task: {task[:100]}",
            backstory=f"You are {AGENT_NAME}, a helpful AI agent powered by CrewAI.",
            llm=llm,
            tools=tools if tools else [],
            verbose=False,
            allow_delegation=False,
        )

        task_obj = Task(
            description=task,
            expected_output="A clear and concise answer to the user's request.",
            agent=agent,
        )

        crew = Crew(agents=[agent], tasks=[task_obj], verbose=False)
        result = await asyncio.to_thread(crew.kickoff)
        return str(result)

    except Exception as e:
        _log_activity("error", f"CrewAI execution failed: {e}")
        return f"[{AGENT_NAME}] Task received: {task[:200]}. (CrewAI execution failed: {e})"


# ── Dashboard (reuse OpenClaw's) ──────────────────────────────────────────
from main import DASHBOARD_HTML


@app.on_event("startup")
async def startup():
    _build_mcp()
    if MCP_MANAGER:
        await MCP_MANAGER.connect_all()


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML.format(agent_name=AGENT_NAME)


@app.get("/status")
async def status():
    return {
        "agent_id": AGENT_ID,
        "name": AGENT_NAME,
        "skills": SKILLS,
        "status": "running",
        "uptime": str(datetime.utcnow() - _start_time),
        "framework": "crewai",
    }


@app.get("/info")
async def info():
    return {
        "agent_id": AGENT_ID,
        "name": AGENT_NAME,
        "skills": SKILLS,
        "status": "running",
    }


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/skills")
async def skills():
    return {"skills": SKILLS}


@app.post("/invoke")
async def invoke(request: Dict):
    task = request.get("task", request.get("input", ""))
    _log_activity("invoke", f"Task: {str(task)[:80]}")
    output = await _call_llm(task, system=f"You are {AGENT_NAME}, a helpful AI agent.")
    return {
        "status": "success",
        "agent_id": AGENT_ID,
        "result": {"output": output},
    }


@app.post("/delegate")
async def delegate(request: Dict):
    delegation_id = request.get("delegation_id", "unknown")
    task = request.get("task", "")
    callback_url = request.get("callback_url")

    _log_activity("delegation", f"Task: {str(task)[:80]}", {"delegation_id": delegation_id})
    asyncio.create_task(_run_delegation(delegation_id, task, callback_url))

    return {
        "status": "in_progress",
        "agent_id": AGENT_ID,
        "delegation_id": delegation_id,
    }


async def _post_progress(delegation_id: str, level: str, message: str, data: dict | None = None):
    if not HIVE_URL or not HIVE_API_KEY:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{HIVE_URL}/api/delegate/{delegation_id}/progress",
                headers={"X-API-Key": HIVE_API_KEY, "Content-Type": "application/json"},
                json={"level": level, "message": message, "data": data or {}},
            )
    except Exception as e:
        _log_activity("error", f"Progress post failed: {e}")


async def _run_delegation(delegation_id: str, task: str, callback_url: str | None):
    try:
        await _post_progress(delegation_id, "thinking", "Reading task and planning steps")
        await asyncio.sleep(0.8)
        await _post_progress(delegation_id, "action", f"Processing: {task[:120]}")
        await asyncio.sleep(0.8)

        llm_configured = any(
            os.getenv(k) for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                                    "OPENROUTER_API_KEY", "GOOGLE_API_KEY")
        )
        if llm_configured:
            await _post_progress(delegation_id, "info", "Calling configured LLM (CrewAI)")
        else:
            await _post_progress(delegation_id, "warning", "No LLM configured — returning stub response")

        await asyncio.sleep(0.5)
        result_payload = {
            "output": await _call_llm(task, system=f"You are {AGENT_NAME}, a helpful AI agent."),
            "agent_id": AGENT_ID,
        }

        await _post_progress(delegation_id, "success", "Task complete")
        await _complete_delegation(delegation_id, result_payload, tokens_used=1.0)
    except Exception as e:
        _log_activity("error", f"Delegation {delegation_id} failed: {e}")
        await _post_progress(delegation_id, "error", f"Execution failed: {e}")
        await _fail_delegation(delegation_id, str(e))


async def _complete_delegation(delegation_id: str, result: dict, tokens_used: float):
    if not HIVE_URL or not HIVE_API_KEY:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{HIVE_URL}/api/delegate/{delegation_id}/complete",
                headers={"X-API-Key": HIVE_API_KEY, "Content-Type": "application/json"},
                json={
                    "delegation_id": delegation_id,
                    "agent_id": AGENT_ID,
                    "output": result.get("output", ""),
                    "tokens_used": tokens_used,
                },
            )
    except Exception as e:
        _log_activity("error", f"Complete delegation failed: {e}")


async def _fail_delegation(delegation_id: str, error: str):
    if not HIVE_URL or not HIVE_API_KEY:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{HIVE_URL}/api/delegate/{delegation_id}/fail",
                headers={"X-API-Key": HIVE_API_KEY, "Content-Type": "application/json"},
                json={"delegation_id": delegation_id, "error": error},
            )
    except Exception as e:
        _log_activity("error", f"Fail delegation failed: {e}")
