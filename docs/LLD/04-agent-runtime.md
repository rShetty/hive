# LLD 04 — Agent Runtime

The agent runtime lives in `docker/agent_app/`. There are **three FastAPI apps** sharing an identical external contract (endpoints, Hive callbacks, env vars), differing only in the `_call_llm` implementation and framework imports.

```
docker/
├── Dockerfile.agent           # legacy "Hermes" image (main:app, port 8000)
├── Dockerfile.openclaw        # OpenClaw image (main:app, port 9000, healthcheck)
├── openclaw_bootstrap.sh      # vestigial — not wired into any CMD
└── agent_app/
    ├── main.py                # OpenClaw reference runtime (855 lines)
    ├── main_crewai.py         # CrewAI variant (411 lines)
    ├── main_langchain.py      # LangChain variant (412 lines)
    ├── mcp_client.py          # shared MCP client + manager (350 lines)
    └── requirements.txt       # full deps (fastapi 0.115.6 + langchain + crewai + litellm)
```

## Shared external contract

All three expose the same endpoints:

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | HTML dashboard (Tailwind + Alpine, inline `DASHBOARD_HTML`) |
| GET | `/dashboard` | redirect to `/` |
| GET | `/status` | `{agent_id, agent_name, status, skills, skills_count, tasks_handled, telegram, llm_provider, mcp_servers, mcp_status, uptime_seconds, activity}` (+ `framework` field in crewai/langchain) |
| GET | `/health?token=` | marketplace verification — `{status, token, agent_id, skills}` (openclaw) / `{ok:true}` (crewai/langchain) |
| GET | `/info` | `{agent_id, name, skills, status}` |
| GET | `/skills` | `{skills: [...]}` |
| POST | `/invoke` | `{task?, input?, context?}` → `{status:"success", agent_id, result:{output}}` |
| POST | `/delegate` | `{delegation_id, task, callback_url?, context?, sync?}` → async `{status:"in_progress",...}` or sync full result |

There is **no `/complete` or `/heartbeat` endpoint on the agent** — those are Hive endpoints the agent *calls*.

## Env vars (all three)

`AGENT_ID`, `AGENT_NAME`, `HIVE_URL`, `HIVE_API_KEY`, `INSTANCE_ID`, `SKILLS` (comma-separated), `SKILL_DEFINITIONS` (JSON list of `{name, display_name, description, definition}`), `MCP_SERVERS` (JSON list of `{name, url, description, headers, transport, command, env}`). LLM keys via `<PROVIDER>_FILE` secret files or `<PROVIDER>_API_KEY` env.

## Hive callbacks (all three)

- **Heartbeat loop** — POSTs to `{HIVE_URL}/api/agent/heartbeat` with `X-API-Key` every **60 s**. Started at startup if `HIVE_URL` + `HIVE_API_KEY` set.
- **Progress posting** — `_post_progress` POSTs to `{HIVE_URL}/api/delegate/{delegation_id}/progress` with body `{level, message, data}`. Levels: `thinking`, `action`, `info`, `warning`, `success`, `error`.
- **Complete delegation** — `_complete_delegation` POSTs to `{HIVE_URL}/api/delegate/{delegation_id}/complete` with `{result, tokens_used}`.
- **Fail delegation** — `_fail_delegation` POSTs to `{HIVE_URL}/api/delegate/{delegation_id}/fail`.

## `main.py` — OpenClaw reference (the canonical one)

**LLM integration** (`main.py:65-203`):
- Provider resolution order via `_resolve_llm` (`:103-129`): **OpenRouter** → OpenAI → Anthropic → Google (first with a key wins).
- Base URLs: OpenRouter `https://openrouter.ai/api/v1`, OpenAI `https://api.openai.com/v1`, Anthropic `https://api.anthropic.com/v1`, Google `https://generativelanguage.googleapis.com/v1beta/openai`.
- Default models: OpenRouter `openai/gpt-4o-mini`, OpenAI `gpt-4o-mini`, Anthropic `claude-3-5-sonnet-latest`, Google `gemini-1.5-flash`. (The docstring at `:69` mentioning `tencent/hy3:free` is stale — code uses `openai/gpt-4o-mini` at `:109`.)
- Model lookup tables `_OPENAI_MODELS`/`_ANTHROPIC_MODELS`/`_GOOGLE_MODELS` (`:71-82`) map friendly names to API model ids.
- **Secret files** — `_secret` (`:85-100`) reads `<ENV>_FILE` before falling back to env. This is how Hive keeps keys out of `docker inspect`.
- **Tool-calling loop** (`_call_llm`, `:132-203`): POSTs `{base_url}/chat/completions`; if model returns `tool_calls`, executes each MCP tool via `MCP_MANAGER.call(name, args)`, appends `role:"tool"` results with `tool_call_id`, re-requests — up to **5 iterations**. `max_tokens=1024`, `timeout=60 s`. Returns stub string if no LLM configured.

**Delegation modes** (`main.py:662-687`):
- `sync=True` → `_run_delegation_sync` (`:705-742`): runs inline, streams `thinking`/`action`/`success` progress, calls `_complete_delegation`, returns `{status:"completed",...}`. On error returns `{status:"failed",...}` and calls `_fail_delegation`.
- `sync=False` (default) → schedules `_run_delegation` (`:745-789`) via `asyncio.create_task`, returns `in_progress` immediately. Adds `asyncio.sleep` delays to simulate staged work; emits richer progress including a `warning` if no LLM is configured.

**Skills** — `_build_system_prompt` (`:605-641`) injects skill `instructions` for skills whose `definition.kind == "prompt"`. Also injects a **Team Context** block (`:616-640`) when `context.team_context` is present — lists team members with `agent_id`/`role`/`reports_to`, max delegation depth, and instructions to delegate via `POST /api/delegate/request` to Hive.

**MCP** — `_build_mcp` (`:46-51`) constructs a global `MCP_MANAGER` from `MCP_SERVERS` env. `MCP_MANAGER.openai_tools()` returns OpenAI-function-style tool specs. `MCP_MANAGER.call(name, args)` executes tools. Status surfaced in `/status`.

**Startup** (`@app.on_event("startup")`, `:835-849`): log activity → `_build_mcp()` → stash running loop on `MCP_MANAGER._main_loop` → `await MCP_MANAGER.connect_all()` → start heartbeat loop.

**Dashboard HTML** (`:209-534`): single-page app polling `/status` every 15 s, updating uptime every 30 s, POSTing chat input to `/invoke`. Uses `basePath()` helper so the dashboard works standalone or behind Hive's `/a/{slug}/` proxy.

## `main_crewai.py` — CrewAI variant

Same env contract; reuses `DASHBOARD_HTML` from main.py (`from main import DASHBOARD_HTML`, `:199`). `/status` adds `"framework": "crewai"`.

- `_resolve_llm` identical.
- `_get_crewai_llm` (`:106-122`) returns a **model string** for litellm; prefixes `openrouter/` when base URL is openrouter.ai so litellm routes correctly.
- `_get_crewai_tools` (`:125-152`) wraps each MCP tool in a `crewai.tools.BaseTool` whose `_run` calls `MCP_MANAGER.call_sync(qualified_name, args)` — the synchronous wrapper, because CrewAI runs tools in worker threads.
- `_call_llm` (`:155-195`): builds a CrewAI `Agent` (role/goal/backstory, `allow_delegation=False`), a `Task`, a `Crew`, runs `crew.kickoff()` inside `asyncio.to_thread`.

**Startup** (`:202-219`): additionally rehydrates `*_API_KEY` env vars from `*_FILE` secret files (litellm reads keys from env, not files). Then `_build_mcp`, set `_main_loop`, `connect_all`, start heartbeat.

**Differences from main.py**:
- `_fail_delegation` sends `{delegation_id, error}` as JSON body (`:406-409`) vs main.py's query-param `reason`.
- `_run_delegation` does NOT take `context` (`:356`) — less rich than main.py.
- `_build_system_prompt` injects prompt-kind skills but **no team context block**.

## `main_langchain.py` — LangChain variant

Same contract; `/status` reports `"framework": "langchain"`.

- `_resolve_llm` identical.
- `_get_langchain_llm` (`:106-123`) returns a `langchain_openai.ChatOpenAI` configured with `model`, `api_key`, `base_url`, `temperature=0`, `max_tokens=1024`.
- `_get_langchain_tools` (`:126-150`) wraps each MCP tool with `@tool(name=..., description=...)`, calling `MCP_MANAGER.call_sync(_qn, args)`. Uses a default-arg closure trick (`_qn: str = qualified_name`) to capture the loop variable.
- `_call_llm` (`:153-204`): if tools exist, builds an `AgentExecutor` via `create_openai_tools_agent` + `ChatPromptTemplate` + `MessagesPlaceholder("agent_scratchpad")`, runs `executor.invoke({"input": task})` in a thread with `max_iterations=5`. On failure **falls back to a direct LLM call** (`:193-200`).

**Startup** (`:212-221`): `_build_mcp`, set `_main_loop`, `connect_all`, start heartbeat. Does **not** do secret-file→env rehydration (LangChain reads the key from the `ChatOpenAI(api_key=...)` arg directly).

Same differences as crewai (`_fail_delegation` JSON body, no `context` in `_run_delegation`, no team context in system prompt).

## `mcp_client.py` — shared MCP client (350 lines)

A from-scratch MCP client (no SDK dependency). Three transports:

- **`http`** (default, streamable HTTP): JSON-RPC POST to `url`; if server responds `text/event-stream`, dispatches SSE frames (`:110-125`).
- **`sse`**: opens long-lived GET stream; server advertises a POST `endpoint` via an `endpoint` event; tracks `Mcp-Session-Id` (`:144-170`).
- **`stdio`**: launches `command` as subprocess, speaks newline-delimited JSON-RPC over stdin/stdout (`:185-213`).

Classes:
- `MCPConnection` (`:48-252`): one server. `_rpc` (`:73-88`) issues JSON-RPC with 30 s timeout, resolves via `_pending` future map. `_initialize` (`:217-231`) sends `initialize` with `protocolVersion: "2024-11-05"`, `clientInfo: {name: "hive-openclaw", version: "1.0"}`, then `notifications/initialized`. `list_tools`/`call_tool` wrap `tools/list` and `tools/call`.
- `MCPManager` (`:255-350`): aggregates tools. `connect_all` (`:264-279`) connects each server, lists tools, builds `tool_index` mapping `"{server_name}__{tool_name}"` → `(connection, tool_def)`. `openai_tools()` (`:281-292`) emits OpenAI function-tool specs. `call(qualified_name, args)` (`:294-303`) splits on `__`. `call_sync` (`:305-326`) is the thread-safe wrapper (submits coroutine to `_main_loop` via `run_coroutine_threadsafe`). `_format_result` (`:328-343`) flattens MCP `content` arrays.

## Dockerfiles

### `docker/Dockerfile.openclaw` (24 lines)
`python:3.11-slim`, installs `curl`, pip installs `fastapi==0.109.0, uvicorn[standard]==0.27.0, httpx==0.26.0, pydantic==2.5.0` (inline, **not** from `agent_app/requirements.txt`), `COPY agent_app/ /app/`, `EXPOSE 9000`, healthcheck on `/`, CMD `uvicorn main:app --port 9000`. **Does not ship crewai/langchain** — only the `openclaw` framework works in this image.

### `docker/Dockerfile.agent` (28 lines)
Legacy "Hermes" image. `python:3.11-slim`, installs `curl git`, pip installs `fastapi==0.109.0, uvicorn[standard]==0.27.0, httpx==0.26.0, python-dotenv==1.0.0`, `COPY agent_app/`, `EXPOSE 8000`, CMD `uvicorn main:app --port 8000`. No healthcheck. Referenced by `AGENT_IMAGE` (default `hive-agent:latest`).

### Dependency notes
- `docker/agent_app/requirements.txt` pins `fastapi==0.115.6` and bundles crewai/langchain/litellm, but **neither Dockerfile installs from it**. The Dockerfiles pin smaller inline sets. To run `langchain`/`crewai` frameworks you need the requirements.txt deps — which only happens in the local-subprocess path (Path D, [LLD/08](08-deploy-paths.md)) where the Hive backend venv is reused.
- Version skew: 0.115.6 in requirements.txt vs 0.109.0 in the Dockerfiles.
- `docker/openclaw_bootstrap.sh` is present but not invoked by any Dockerfile CMD or deploy path — vestigial.

## Known inconsistencies

1. **`/fail` callback contract** — `main.py` passes `reason` as a query param (`:817-818`); `main_crewai.py`/`main_langchain.py` pass `{delegation_id, error}` as JSON body (`:406-409`). The three runtimes disagree.
2. **`main.py:69` docstring** says default model is `tencent/hy3:free` but code uses `openai/gpt-4o-mini` (`:109`) — stale docstring.
3. **`main_langchain.py:199`** calls `asyncio.ainvoke(llm, messages)` — `ainvoke` is a bound method on LangChain runnables, not a free function; this fallback branch will raise if hit.
4. **Team context** — only `main.py` injects the team-context block into the system prompt; crewai/langchain variants do not, so team delegation planning works best with the openclaw framework.
5. **Port mismatch** — `Dockerfile.openclaw` listens on 9000, but `OPENCLAW_INTERNAL_PORT` is `8080` in `container_manager.py:197` (local Docker path) vs `9000` in `openclaw_deployer.py:16` (VPS path).
