"""Hive LangChain agent — HTTP API + web dashboard.

Uses LangChain's agent framework for LLM orchestration and tool calling.
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

app = FastAPI(title="LangChain Agent")

AGENT_ID = os.getenv("AGENT_ID", "unknown")
AGENT_NAME = os.getenv("AGENT_NAME", "Unknown Agent")
SKILLS = [s for s in os.getenv("SKILLS", "").split(",") if s]
SKILL_DEFINITIONS: list[dict] = []
try:
    _sd = json.loads(os.getenv("SKILL_DEFINITIONS", "[]") or "[]")
    if isinstance(_sd, list):
        SKILL_DEFINITIONS = _sd
except Exception:
    pass
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


def _get_langchain_llm():
    """Get a LangChain ChatOpenAI instance from the configured LLM."""
    try:
        from langchain_openai import ChatOpenAI
    except ImportError:
        raise RuntimeError("langchain-openai is not installed")

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


def _get_langchain_tools():
    """Convert MCP tools to LangChain tools."""
    try:
        from langchain_core.tools import tool
    except ImportError:
        raise RuntimeError("langchain-core is not installed")

    if not MCP_MANAGER:
        return []

    tools = []
    for qualified_name, (_conn, t) in MCP_MANAGER.tool_index.items():
        tool_desc = t.get("description", "") or f"Tool {t.get('name')}"

        @tool(name=qualified_name, description=tool_desc)
        def _tool_func(arguments: str = "{}", _qn: str = qualified_name) -> str:
            try:
                args = json.loads(arguments) if isinstance(arguments, str) else arguments
            except Exception:
                args = {}
            return MCP_MANAGER.call_sync(_qn, args)

        tools.append(_tool_func)

    return tools


async def _call_llm(task: str, system: str = "") -> str:
    """Call the configured LLM via LangChain, executing any MCP tools."""
    try:
        llm = _get_langchain_llm()
    except RuntimeError as e:
        # CodeQL py/stack-trace-exposure (#9): exception text never reaches
        # clients — operators get the detail in the activity log, callers get
        # static guidance.
        from hive_client import sanitize_log_text
        _log_activity("error", f"LLM config error: {sanitize_log_text(e)}")
        return f"[{AGENT_NAME}] Task received: {task[:200]}. (No LLM configured — connect one via Hive)"

    if not llm:
        return (
            f"[{AGENT_NAME}] Task received: {task[:200]}. "
            "Connect an LLM via Hive to enable real task execution."
        )

    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        messages = []
        if system:
            messages.append(SystemMessage(content=system))
        messages.append(HumanMessage(content=task))

        tools = _get_langchain_tools()

        if tools:
            try:
                from langchain.agents import AgentExecutor, create_openai_tools_agent
                from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

                prompt = ChatPromptTemplate.from_messages([
                    ("system", system or f"You are {AGENT_NAME}, a helpful AI agent."),
                    ("user", "{input}"),
                    MessagesPlaceholder(variable_name="agent_scratchpad"),
                ])

                agent = create_openai_tools_agent(llm, tools, prompt)
                executor = AgentExecutor(agent=agent, tools=tools, verbose=False, max_iterations=5)
                result = await asyncio.to_thread(
                    executor.invoke, {"input": task}
                )
                return result.get("output", str(result))
            except Exception as e:
                _log_activity("error", f"LangChain agent executor failed: {e}")
                # Fall back to direct LLM call
                pass

        # Simple LLM call without tools
        result = await asyncio.ainvoke(llm, messages)
        return result.content.strip()

    except Exception as e:
        # CodeQL py/stack-trace-exposure (#9): details stay in the log.
        from hive_client import sanitize_log_text
        _log_activity("error", f"LLM call failed: {sanitize_log_text(e)}")
        return f"[{AGENT_NAME}] Task received: {task[:200]}. (LLM call failed)"


# ── Dashboard (reuse OpenClaw's) ──────────────────────────────────────────
# Import dashboard HTML from main.py
from main import DASHBOARD_HTML


@app.on_event("startup")
async def startup():
    import asyncio as _asyncio
    _build_mcp()
    if MCP_MANAGER:
        MCP_MANAGER._main_loop = _asyncio.get_running_loop()
        await MCP_MANAGER.connect_all()
    # Start heartbeat loop
    if HIVE_URL and HIVE_API_KEY:
        _asyncio.create_task(_heartbeat_loop())


async def _send_heartbeat():
    if not HIVE_URL or not HIVE_API_KEY:
        return
    try:
        async with httpx.AsyncClient() as client:
            await client.post(f"{HIVE_URL}/api/agent/heartbeat",
                              headers={"X-API-Key": HIVE_API_KEY}, timeout=10.0)
    except Exception:
        pass


async def _heartbeat_loop():
    while True:
        await _send_heartbeat()
        await asyncio.sleep(60)


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML.format(
        agent_name=AGENT_NAME,
        agent_id=AGENT_ID,
        hive_url=os.getenv("MARKETPLACE_URL") or os.getenv("HIVE_URL", "https://hive.rajeev.me"),
        start_time=_start_time.isoformat(),
    )


@app.get("/status")
async def status():
    return {
        "agent_id": AGENT_ID,
        "name": AGENT_NAME,
        "skills": SKILLS,
        "status": "running",
        "uptime": str(datetime.utcnow() - _start_time),
        "framework": "langchain",
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


def _build_system_prompt(base: str = "") -> str:
    prompt = base or f"You are {AGENT_NAME}, a helpful AI agent."
    skill_instructions = [
        s["definition"]["instructions"]
        for s in SKILL_DEFINITIONS
        if s.get("definition", {}).get("kind") == "prompt" and s["definition"].get("instructions")
    ]
    if skill_instructions:
        prompt += "\n\nYour capabilities:\n" + "\n".join(f"- {inst}" for inst in skill_instructions)
    return prompt


@app.post("/invoke")
async def invoke(request: Dict):
    task = request.get("task", request.get("input", ""))
    _log_activity("invoke", f"Task: {str(task)[:80]}")
    output = await _call_llm(task, system=_build_system_prompt())
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
    sync = request.get("sync", False)
    context = request.get("context", {})

    _log_activity("delegation", f"Task: {str(task)[:80]}", {"delegation_id": delegation_id})
    if sync:
        return await _run_delegation_sync(delegation_id, task, callback_url, context)
    asyncio.create_task(_run_delegation(delegation_id, task, callback_url))

    return {
        "status": "in_progress",
        "agent_id": AGENT_ID,
        "delegation_id": delegation_id,
    }


async def _run_delegation_sync(delegation_id: str, task: str, callback_url: str | None, context: dict) -> dict:
    try:
        await _post_progress(delegation_id, "info", "Processing task")
        await asyncio.sleep(0.3)
        system_prompt = _build_system_prompt()
        output = await _call_llm(task, system=system_prompt)
        result_payload = {"output": output, "agent_id": AGENT_ID}
        await _post_progress(delegation_id, "success", "Task complete (sync)")
        await _complete_delegation(delegation_id, result_payload, tokens_used=1.0)
        return {"status": "completed", "agent_id": AGENT_ID, "delegation_id": delegation_id, "result": result_payload}
    except Exception as e:
        from hive_client import sanitize_log_text
        reason = sanitize_log_text(e)
        await _post_progress(delegation_id, "error", f"Task failed: {reason}")
        await _fail_delegation(delegation_id, reason)
        return {"status": "failed", "agent_id": AGENT_ID, "delegation_id": delegation_id, "error": reason}


async def _post_progress(delegation_id: str, level: str, message: str, data: dict | None = None):
    if not HIVE_URL or not HIVE_API_KEY:
        return
    # CodeQL py/partial-ssrf (#14): validated base + allowlisted delegation id.
    from hive_client import hive_callback_url
    url = hive_callback_url("api/delegate", delegation_id, "progress")
    if not url:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                url,
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
            await _post_progress(delegation_id, "info", "Calling configured LLM (LangChain)")
        else:
            await _post_progress(delegation_id, "warning", "No LLM configured — returning stub response")

        await asyncio.sleep(0.5)
        result_payload = {
            "output": await _call_llm(task, system=_build_system_prompt()),
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
    # CodeQL py/partial-ssrf (#20): validated base + allowlisted id.
    from hive_client import hive_callback_url
    url = hive_callback_url("api/delegate", delegation_id, "complete")
    if not url:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                url,
                headers={"X-API-Key": HIVE_API_KEY, "Content-Type": "application/json"},
                json={"result": result, "tokens_used": tokens_used},
            )
    except Exception as e:
        _log_activity("error", f"Complete delegation failed: {e}")


async def _fail_delegation(delegation_id: str, error: str):
    if not HIVE_URL or not HIVE_API_KEY:
        return
    # CodeQL py/partial-ssrf (#21): validated base + allowlisted id.
    from hive_client import hive_callback_url
    url = hive_callback_url("api/delegate", delegation_id, "fail")
    if not url:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                url,
                headers={"X-API-Key": HIVE_API_KEY, "Content-Type": "application/json"},
                json={"delegation_id": delegation_id, "error": error},
            )
    except Exception as e:
        _log_activity("error", f"Fail delegation failed: {e}")
