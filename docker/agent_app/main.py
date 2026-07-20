"""Hive OpenClaw agent — HTTP API + web dashboard."""
import asyncio
import os
import json
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
import httpx

app = FastAPI(title="OpenClaw Agent")

AGENT_ID = os.getenv("AGENT_ID", "unknown")
AGENT_NAME = os.getenv("AGENT_NAME", "Unknown Agent")
SKILLS = [s for s in os.getenv("SKILLS", "").split(",") if s]
HIVE_URL = os.getenv("HIVE_URL", "")
HIVE_API_KEY = os.getenv("HIVE_API_KEY", "")
INSTANCE_ID = os.getenv("INSTANCE_ID", "")

# MCP servers configured for this agent (list of {name, url, description, headers})
try:
    MCP_SERVERS = json.loads(os.getenv("MCP_SERVERS", "[]") or "[]")
    if not isinstance(MCP_SERVERS, list):
        MCP_SERVERS = []
except Exception:
    MCP_SERVERS = []

# Track recent activity in-memory
_activity: list[dict] = []
_start_time = datetime.utcnow()


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
# OpenClaw agents can call a real LLM when credentials are present. OpenRouter
# is the default (single key, many models); Anthropic/OpenAI/Google also work
# if their keys are set. The model id comes from OPENROUTER_MODEL (default
# tencent/hy3:free) or the provider-specific defaults below.

_OPENAI_MODELS = {
    "chatgpt-4o": "gpt-4o",
    "gpt-4o-mini": "gpt-4o-mini",
}
_ANTHROPIC_MODELS = {
    "claude-3-5-sonnet": "claude-3-5-sonnet-latest",
    "claude-3-haiku": "claude-3-haiku-20240307",
}
_GOOGLE_MODELS = {
    "gemini-1.5-pro": "gemini-1.5-pro",
    "gemini-1.5-flash": "gemini-1.5-flash",
}


def _secret(provider_env: str) -> Optional[str]:
    """Read an API key from an env var or, if set, from a secret file.

    A ``*_API_KEY_FILE`` env var pointing at a file takes precedence; the file
    contents (stripped) are used as the key so secrets can be mounted as files
    instead of being exposed in the container's environment.
    """
    file_env = provider_env + "_FILE"
    path = os.getenv(file_env)
    if path and os.path.isfile(path):
        try:
            with open(path, "r") as fh:
                return fh.read().strip()
        except OSError:
            return None
    return os.getenv(provider_env)


def _resolve_llm() -> Optional[dict]:
    """Return (base_url, api_key, model) for the first configured provider."""
    if _secret("OPENROUTER_API_KEY"):
        return {
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": os.getenv("OPENROUTER_API_KEY"),
            "model": os.getenv("OPENROUTER_MODEL", "tencent/hy3:free"),
        }
    if _secret("OPENAI_API_KEY"):
        return {
            "base_url": "https://api.openai.com/v1",
            "api_key": _secret("OPENAI_API_KEY"),
            "model": _OPENAI_MODELS.get(os.getenv("OPENAI_MODEL", "gpt-4o-mini"), "gpt-4o-mini"),
        }
    if _secret("ANTHROPIC_API_KEY"):
        return {
            "base_url": "https://api.anthropic.com/v1",
            "api_key": _secret("ANTHROPIC_API_KEY"),
            "model": _ANTHROPIC_MODELS.get(os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet"), "claude-3-5-sonnet-latest"),
        }
    if _secret("GOOGLE_API_KEY"):
        return {
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "api_key": _secret("GOOGLE_API_KEY"),
            "model": _GOOGLE_MODELS.get(os.getenv("GOOGLE_MODEL", "gemini-1.5-flash"), "gemini-1.5-flash"),
        }
    return None


async def _call_llm(task: str, system: str = "") -> str:
    """Call the configured LLM and return its text response."""
    cfg = _resolve_llm()
    if not cfg:
        return (
            f"[{AGENT_NAME}] Task received: {task[:200]}. "
            "Connect an LLM via Hive to enable real task execution."
        )

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": task})

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{cfg['base_url']}/chat/completions",
                headers={"Authorization": f"Bearer {cfg['api_key']}"},
                json={"model": cfg["model"], "messages": messages, "max_tokens": 1024},
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        _log_activity("error", f"LLM call failed: {e}")
        return f"[{AGENT_NAME}] Task received: {task[:200]}. (LLM call failed: {e})"



# ── Dashboard HTML ─────────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{agent_name} — Agent Dashboard</title>
<script src="https://cdn.tailwindcss.com"></script>
<script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
  body {{ font-family: 'Inter', sans-serif; }}
  .pulse-dot {{ animation: pulse 2s cubic-bezier(0.4,0,0.6,1) infinite; }}
  @keyframes pulse {{ 0%,100%{{opacity:1}} 50%{{opacity:.5}} }}
  .chat-bubble-user {{ background: #4f46e5; color: white; border-radius: 18px 18px 4px 18px; }}
  .chat-bubble-agent {{ background: #f3f4f6; color: #1f2937; border-radius: 18px 18px 18px 4px; }}
  .scroll-smooth {{ scroll-behavior: smooth; }}
</style>
</head>
<body class="bg-gray-50 min-h-screen" x-data="agentDash()">

<!-- Header -->
<header class="bg-white border-b sticky top-0 z-10">
  <div class="max-w-5xl mx-auto px-4 sm:px-6 h-14 flex items-center justify-between">
    <div class="flex items-center space-x-3">
      <div class="w-8 h-8 bg-purple-600 rounded-lg flex items-center justify-center">
        <span class="text-white text-sm font-bold">AI</span>
      </div>
      <div>
        <span class="font-semibold text-gray-900 text-sm">{agent_name}</span>
        <span class="text-gray-400 text-xs ml-2">· OpenClaw Agent</span>
      </div>
    </div>
    <div class="flex items-center space-x-3">
      <div class="flex items-center space-x-1.5">
        <div :class="online ? 'bg-green-400' : 'bg-gray-300'"
             class="w-2 h-2 rounded-full pulse-dot"></div>
        <span class="text-xs text-gray-500" x-text="online ? 'Online' : 'Offline'"></span>
      </div>
      <a href="{hive_url}" target="_blank"
         class="text-xs text-indigo-600 hover:text-indigo-800 font-medium">
        Hive Marketplace →
      </a>
    </div>
  </div>
</header>

<div class="max-w-5xl mx-auto px-4 sm:px-6 py-6 space-y-6">

  <!-- Status bar -->
  <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
    <div class="bg-white rounded-xl border p-4">
      <div class="text-xs text-gray-500 mb-1">Status</div>
      <div class="flex items-center space-x-2">
        <div :class="online ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-500'"
             class="text-sm font-medium px-2 py-0.5 rounded-full" x-text="online ? 'Active' : 'Starting'"></div>
      </div>
    </div>
    <div class="bg-white rounded-xl border p-4">
      <div class="text-xs text-gray-500 mb-1">Uptime</div>
      <div class="text-sm font-semibold text-gray-900" x-text="uptime"></div>
    </div>
    <div class="bg-white rounded-xl border p-4">
      <div class="text-xs text-gray-500 mb-1">Tasks Handled</div>
      <div class="text-sm font-semibold text-gray-900" x-text="stats.tasks_handled"></div>
    </div>
    <div class="bg-white rounded-xl border p-4">
      <div class="text-xs text-gray-500 mb-1">Skills</div>
      <div class="text-sm font-semibold text-gray-900" x-text="stats.skills_count"></div>
    </div>
  </div>

  <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">

    <!-- Chat panel -->
    <div class="lg:col-span-2 bg-white rounded-xl border flex flex-col" style="height: 520px">
      <div class="p-4 border-b flex items-center space-x-2">
        <div class="w-7 h-7 bg-purple-100 rounded-full flex items-center justify-center text-purple-600 text-xs font-bold">AI</div>
        <div>
          <div class="text-sm font-semibold text-gray-900">{agent_name}</div>
          <div class="text-xs text-gray-400">Send a task or ask a question</div>
        </div>
      </div>

      <!-- Messages -->
      <div class="flex-1 overflow-y-auto p-4 space-y-3 scroll-smooth" id="chat-scroll">
        <div class="flex items-start space-x-2">
          <div class="w-6 h-6 bg-purple-100 rounded-full flex items-center justify-center text-purple-600 text-xs font-bold flex-shrink-0 mt-0.5">AI</div>
          <div class="chat-bubble-agent px-4 py-2.5 text-sm max-w-xs">
            Hi! I'm {agent_name}. Send me a task and I'll get to work.
          </div>
        </div>
        <template x-for="msg in messages" :key="msg.id">
          <div :class="msg.role === 'user' ? 'flex justify-end' : 'flex items-start space-x-2'">
            <template x-if="msg.role === 'agent'">
              <div class="w-6 h-6 bg-purple-100 rounded-full flex items-center justify-center text-purple-600 text-xs font-bold flex-shrink-0 mt-0.5">AI</div>
            </template>
            <div :class="msg.role === 'user' ? 'chat-bubble-user px-4 py-2.5 text-sm max-w-xs ml-2' : 'chat-bubble-agent px-4 py-2.5 text-sm max-w-xs'"
                 x-text="msg.text"></div>
          </div>
        </template>
        <div x-show="thinking" class="flex items-start space-x-2">
          <div class="w-6 h-6 bg-purple-100 rounded-full flex items-center justify-center text-purple-600 text-xs font-bold flex-shrink-0 mt-0.5">AI</div>
          <div class="chat-bubble-agent px-4 py-2.5 text-sm">
            <span class="inline-flex space-x-1">
              <span class="animate-bounce" style="animation-delay:0s">·</span>
              <span class="animate-bounce" style="animation-delay:0.15s">·</span>
              <span class="animate-bounce" style="animation-delay:0.3s">·</span>
            </span>
          </div>
        </div>
      </div>

      <!-- Input -->
      <div class="p-3 border-t flex space-x-2">
        <input type="text" x-model="chatInput"
               @keyup.enter="sendMessage()"
               placeholder="Ask your agent something..."
               class="flex-1 px-4 py-2 text-sm border border-gray-200 rounded-full focus:outline-none focus:ring-2 focus:ring-purple-400">
        <button @click="sendMessage()" :disabled="thinking || !chatInput.trim()"
                class="w-9 h-9 bg-purple-600 text-white rounded-full flex items-center justify-center hover:bg-purple-700 disabled:opacity-40 flex-shrink-0">
          <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8"/>
          </svg>
        </button>
      </div>
    </div>

    <!-- Sidebar -->
    <div class="space-y-4">

      <!-- Skills -->
      <div class="bg-white rounded-xl border p-4">
        <h3 class="text-sm font-semibold text-gray-700 mb-3">Skills</h3>
        <div x-show="stats.skills.length === 0" class="text-xs text-gray-400">No skills configured</div>
        <div class="space-y-2">
          <template x-for="skill in stats.skills" :key="skill">
            <div class="flex items-center space-x-2">
              <div class="w-6 h-6 bg-indigo-50 rounded-md flex items-center justify-center text-xs">⚙️</div>
              <span class="text-xs text-gray-700" x-text="skill"></span>
            </div>
          </template>
        </div>
      </div>

      <!-- Integrations -->
      <div class="bg-white rounded-xl border p-4">
        <h3 class="text-sm font-semibold text-gray-700 mb-3">Integrations</h3>
        <div class="space-y-2">
          <div class="flex items-center justify-between">
            <div class="flex items-center space-x-2">
              <span class="text-base">✈️</span>
              <span class="text-xs text-gray-700">Telegram</span>
            </div>
            <span :class="stats.telegram ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-400'"
                  class="text-xs px-2 py-0.5 rounded-full font-medium"
                  x-text="stats.telegram ? 'Connected' : 'Not set'"></span>
          </div>
          <div class="flex items-center justify-between">
            <div class="flex items-center space-x-2">
              <span class="text-base">🤖</span>
              <span class="text-xs text-gray-700">LLM</span>
            </div>
            <span :class="stats.llm_provider ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-400'"
                  class="text-xs px-2 py-0.5 rounded-full font-medium"
                  x-text="stats.llm_provider || 'Not set'"></span>
          </div>
        </div>
      </div>

      <!-- MCP Servers -->
      <div class="bg-white rounded-xl border p-4" x-show="stats.mcp_servers.length > 0">
        <h3 class="text-sm font-semibold text-gray-700 mb-3">MCP Servers</h3>
        <div class="space-y-2">
          <template x-for="mcp in stats.mcp_servers" :key="mcp.name">
            <div class="flex items-center space-x-2">
              <div class="w-6 h-6 bg-purple-50 rounded-md flex items-center justify-center text-xs">🔌</div>
              <div class="min-w-0">
                <div class="text-xs text-gray-700 font-medium" x-text="mcp.name"></div>
                <div class="text-xs text-gray-400 truncate" x-text="mcp.url"></div>
              </div>
            </div>
          </template>
        </div>
      </div>

      <!-- Recent Activity -->
      <div class="bg-white rounded-xl border p-4">
        <h3 class="text-sm font-semibold text-gray-700 mb-3">Recent Activity</h3>
        <div x-show="activity.length === 0" class="text-xs text-gray-400">No activity yet</div>
        <div class="space-y-2">
          <template x-for="item in activity.slice(0, 5)" :key="item.ts">
            <div class="flex items-start space-x-2">
              <div :class="item.kind === 'error' ? 'bg-red-100 text-red-500' : item.kind === 'delegation' ? 'bg-purple-100 text-purple-600' : 'bg-gray-100 text-gray-500'"
                   class="w-5 h-5 rounded flex items-center justify-center text-xs flex-shrink-0 mt-0.5">
                <span x-text="item.kind === 'delegation' ? '→' : item.kind === 'error' ? '!' : '·'"></span>
              </div>
              <div>
                <div class="text-xs text-gray-700" x-text="item.summary"></div>
                <div class="text-xs text-gray-400" x-text="timeAgo(item.ts)"></div>
              </div>
            </div>
          </template>
        </div>
      </div>

      <!-- API Info -->
      <div class="bg-white rounded-xl border p-4">
        <h3 class="text-sm font-semibold text-gray-700 mb-3">API</h3>
        <div class="space-y-1.5 text-xs">
          <div>
            <span class="text-gray-500">Agent ID:</span>
            <code class="ml-1 bg-gray-100 px-1 rounded text-gray-700" x-text="agentId"></code>
          </div>
          <div>
            <span class="text-gray-500">Invoke:</span>
            <code class="ml-1 bg-gray-100 px-1 rounded text-gray-700">POST /invoke</code>
          </div>
          <div>
            <span class="text-gray-500">Delegate:</span>
            <code class="ml-1 bg-gray-100 px-1 rounded text-gray-700">POST /delegate</code>
          </div>
        </div>
      </div>

    </div>
  </div>
</div>

<script>
function agentDash() {{
  return {{
    online: false,
    uptime: '—',
    chatInput: '',
    messages: [],
    thinking: false,
    _msgId: 0,
    stats: {{ tasks_handled: 0, skills_count: 0, skills: [], telegram: false, llm_provider: null, mcp_servers: [] }},
    activity: [],
    agentId: '{agent_id}',

    async init() {{
      await this.poll();
      setInterval(() => this.poll(), 15000);
      this.updateUptime();
      setInterval(() => this.updateUptime(), 30000);
    }},

    async poll() {{
      try {{
        const r = await fetch('/status');
        if (r.ok) {{
          const d = await r.json();
          this.online = true;
          this.stats = d;
          this.activity = d.activity || [];
        }}
      }} catch (_) {{ this.online = false; }}
    }},

    updateUptime() {{
      const started = new Date('{start_time}');
      const diff = Math.floor((Date.now() - started) / 1000);
      const h = Math.floor(diff / 3600);
      const m = Math.floor((diff % 3600) / 60);
      this.uptime = h > 0 ? `${{h}}h ${{m}}m` : `${{m}}m`;
    }},

    async sendMessage() {{
      const text = this.chatInput.trim();
      if (!text) return;
      this.chatInput = '';
      this.messages.push({{ id: ++this._msgId, role: 'user', text }});
      this.thinking = true;
      this.$nextTick(() => {{
        const el = document.getElementById('chat-scroll');
        if (el) el.scrollTop = el.scrollHeight;
      }});

      try {{
        const r = await fetch('/invoke', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ task: text, context: {{}} }}),
        }});
        const d = await r.json();
        const reply = d.result?.output || d.result || 'Task processed.';
        this.messages.push({{ id: ++this._msgId, role: 'agent', text: typeof reply === 'string' ? reply : JSON.stringify(reply) }});
      }} catch (e) {{
        this.messages.push({{ id: ++this._msgId, role: 'agent', text: 'Error: could not reach agent.' }});
      }} finally {{
        this.thinking = false;
        this.$nextTick(() => {{
          const el = document.getElementById('chat-scroll');
          if (el) el.scrollTop = el.scrollHeight;
        }});
      }}
    }},

    timeAgo(ts) {{
      const diff = Math.floor((Date.now() - new Date(ts)) / 1000);
      if (diff < 60) return 'just now';
      if (diff < 3600) return Math.floor(diff/60) + 'm ago';
      return Math.floor(diff/3600) + 'h ago';
    }},
  }};
}}
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Serve the agent web dashboard."""
    llm_provider = None
    for env_key, label in [("ANTHROPIC_API_KEY", "Claude"), ("OPENAI_API_KEY", "OpenAI"),
                            ("OPENROUTER_API_KEY", "OpenRouter"), ("GOOGLE_API_KEY", "Google")]:
        if os.getenv(env_key):
            llm_provider = label
            break

    html = DASHBOARD_HTML.format(
        agent_name=AGENT_NAME,
        agent_id=AGENT_ID,
        hive_url=HIVE_URL or "https://hive.rajeev.me",
        start_time=_start_time.isoformat(),
    )
    return HTMLResponse(content=html)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_redirect():
    return await dashboard()


@app.get("/status")
async def status():
    """Live status for the dashboard polling."""
    llm_provider = None
    for env_key, label in [("ANTHROPIC_API_KEY", "Claude"), ("OPENAI_API_KEY", "OpenAI"),
                            ("OPENROUTER_API_KEY", "OpenRouter"), ("GOOGLE_API_KEY", "Google")]:
        if os.getenv(env_key):
            llm_provider = label
            break

    return {
        "agent_id": AGENT_ID,
        "agent_name": AGENT_NAME,
        "status": "running",
        "skills": SKILLS,
        "skills_count": len(SKILLS),
        "tasks_handled": sum(1 for a in _activity if a["kind"] == "delegation"),
        "telegram": bool(os.getenv("TELEGRAM_BOT_TOKEN")),
        "llm_provider": llm_provider,
        "mcp_servers": [{"name": m.get("name"), "url": m.get("url")} for m in MCP_SERVERS],
        "uptime_seconds": int((datetime.utcnow() - _start_time).total_seconds()),
        "activity": _activity[:20],
    }


@app.get("/health")
async def health_check(token: str = ""):
    """Health check endpoint for marketplace verification."""
    return {"status": "healthy", "token": token, "agent_id": AGENT_ID, "skills": SKILLS}


@app.get("/info")
async def root():
    """Agent info (JSON)."""
    return {"agent_id": AGENT_ID, "name": AGENT_NAME, "skills": SKILLS, "status": "running"}


@app.get("/skills")
async def list_skills():
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
    """Hive delegation endpoint.

    Returns ``in_progress`` immediately so Hive can respond to the requester
    in <100 ms; the real work runs in a background task that pushes progress
    updates over HTTP while executing and signs a completion callback at the
    end. This exercises the full streaming path on the Hive side.
    """
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
    """Send a progress update to Hive so it streams out over SSE."""
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
    """Execute the delegation in the background with streamed progress."""
    try:
        await _post_progress(delegation_id, "thinking", "Reading task and planning steps")
        await asyncio.sleep(0.8)

        await _post_progress(
            delegation_id,
            "action",
            f"Processing: {task[:120]}",
        )
        await asyncio.sleep(0.8)

        llm_configured = any(
            os.getenv(k) for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                                    "OPENROUTER_API_KEY", "GOOGLE_API_KEY")
        )
        if llm_configured:
            await _post_progress(delegation_id, "info", "Calling configured LLM")
        else:
            await _post_progress(
                delegation_id,
                "warning",
                "No LLM configured — returning stub response",
            )

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
    """Call Hive's /complete endpoint (API-key auth) to settle the delegation."""
    if not HIVE_URL or not HIVE_API_KEY:
        return
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{HIVE_URL}/api/delegate/{delegation_id}/complete",
                headers={"X-API-Key": HIVE_API_KEY, "Content-Type": "application/json"},
                json={"result": result, "tokens_used": tokens_used},
            )
            if resp.status_code >= 400:
                _log_activity("error", f"Complete {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        _log_activity("error", f"Complete failed: {e}")


async def _fail_delegation(delegation_id: str, reason: str):
    """Call Hive's /fail endpoint so tokens get refunded."""
    if not HIVE_URL or not HIVE_API_KEY:
        return
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(
                f"{HIVE_URL}/api/delegate/{delegation_id}/fail",
                headers={"X-API-Key": HIVE_API_KEY},
                params={"reason": reason[:200]},
            )
    except Exception as e:
        _log_activity("error", f"Fail callback errored: {e}")


async def _send_heartbeat():
    if not HIVE_URL or not HIVE_API_KEY:
        return
    try:
        async with httpx.AsyncClient() as client:
            await client.post(f"{HIVE_URL}/api/agent/heartbeat",
                              headers={"X-API-Key": HIVE_API_KEY}, timeout=10.0)
    except Exception as e:
        _log_activity("error", f"Heartbeat failed: {e}")


@app.on_event("startup")
async def startup_event():
    _log_activity("start", f"Agent {AGENT_NAME} started")
    if HIVE_URL and HIVE_API_KEY:
        asyncio.create_task(_heartbeat_loop())


async def _heartbeat_loop():
    while True:
        await _send_heartbeat()
        await asyncio.sleep(60)
