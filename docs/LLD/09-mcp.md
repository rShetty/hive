# LLD 09 — MCP Integration

Hive has full Model Context Protocol (MCP) support: a server registry, per-agent grants, header auth or OAuth 2.0 (PKCE + DCR), and a from-scratch MCP client in the agent runtime.

## Registry — `/api/mcp-servers` (`routers/mcp.py`)

### Data model (`models/mcp.py`)
- **MCPServer** (`:13`): `owner_id`, `name`, `url`, `description`, `transport` (http|sse|stdio), `command` (stdio), `env_encrypted`, `headers_encrypted`, `auth_type` (headers|oauth), `oauth_encrypted` (access/refresh/expiry/client_id/secret/issuer/scope), `oauth_client_id/secret`, `oauth_scopes`, `visibility` (private|platform), `is_active`, timestamps. `access_grants` cascade.
- **AgentMCPAccess** (`:70`): `agent_id`, `mcp_server_id`, `headers_encrypted` (per-agent override), `enabled`, `created_at`.

### Endpoints
| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `` | JWT | own + platform catalog (`is_catalog`/`granted` flags) |
| POST | `` | JWT | validate URL; admin-only for `visibility=platform` |
| GET/PUT/DELETE | `/{id}` | JWT (owner) | CRUD |
| GET | `/{id}/agents` | JWT | which agents granted |
| POST | `/{id}/grant` | JWT | `AgentMCPGrantRequest` (idempotent, per-agent header overrides) |
| POST | `/{id}/revoke` | JWT | revoke |
| GET | `/agent/{agent_id}` | JWT | servers for an agent |

Helpers: `_get_owned_server`, `_get_accessible_server` (own or platform), `_get_owned_agent`.

### Schema validation (`schemas.py:220`)
`MCPServerCreate` `model_validator`: stdio transport requires `command`; http/sse transport requires `url`. `auth_type` must match. `visibility=platform` is admin-only.

## OAuth — `/api/mcp` (`routers/mcp_oauth.py`)

OAuth 2.0 connect flow with PKCE + Dynamic Client Registration (DCR). In-memory `_STATE` cache.

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/servers/{id}/connect` | JWT | `_discover` (`.well-known/oauth-authorization-server`, GitHub hardcoded) → `_register_client` (DCR → static creds → cached DCR) → generate PKCE → return `authorize_url` |
| GET | `/oauth/callback` | public | exchange code, store encrypted token blob on `MCPServer.oauth_encrypted`, redirect to `/mcp?connected={id}` |
| POST | `/servers/{id}/refresh` | JWT | refresh access token |

Encrypted token blob stores: access/refresh/expiry/client_id/secret/issuer/scope. GitHub is special-cased (hardcoded endpoints).

## How MCP servers reach agents

At deploy time (BYOK hosted, Path D; or OpenClaw deploy), the granted MCP servers are serialized into the `MCP_SERVERS` env var as a JSON list of `{name, url, description, headers, transport, command, env}`. The agent runtime's `_build_mcp()` parses this and constructs a `MCPManager`.

Per-agent header overrides (from `AgentMCPAccess.headers_encrypted`) and OAuth tokens (from `MCPServer.oauth_encrypted`) are decrypted by Hive and injected into the `headers` field of the serialized MCP server spec.

## Agent runtime MCP client — `docker/agent_app/mcp_client.py`

A from-scratch MCP client (no SDK dependency). See [LLD/04](04-agent-runtime.md#mcp_clientpy--shared-mcp-client-350-lines) for the full breakdown. Three transports:

- **`http`** (default, streamable HTTP): JSON-RPC POST; handles SSE response frames.
- **`sse`**: long-lived GET stream; server advertises POST endpoint via `endpoint` event; tracks `Mcp-Session-Id`.
- **`stdio`**: launches `command` as subprocess, newline-delimited JSON-RPC over stdin/stdout.

`MCPConnection._initialize` sends `initialize` with `protocolVersion: "2024-11-05"`, `clientInfo: {name: "hive-openclaw", version: "1.0"}`, then `notifications/initialized`. `list_tools`/`call_tool` wrap `tools/list` and `tools/call`. 30 s timeout per RPC.

`MCPManager` aggregates tools across servers, builds `tool_index` mapping `"{server_name}__{tool_name}"` → `(connection, tool_def)`. `openai_tools()` emits OpenAI function-tool specs. `call(qualified_name, args)` splits on `__`. `call_sync` is the thread-safe wrapper (submits coroutine to `_main_loop` via `run_coroutine_threadsafe`) used by crewai/langchain.

## Tool-calling integration

- **OpenClaw (`main.py`)** — `_call_llm` (`:132-203`) uses `MCP_MANAGER.openai_tools()` as the `tools` param to the LLM `chat/completions` call. If the model returns `tool_calls`, it executes each via `MCP_MANAGER.call(name, args)`, appends `role:"tool"` results, re-requests — up to 5 iterations.
- **CrewAI (`main_crewai.py`)** — `_get_crewai_tools` wraps each MCP tool in a `crewai.tools.BaseTool` whose `_run` calls `MCP_MANAGER.call_sync` (sync wrapper for worker threads).
- **LangChain (`main_langchain.py`)** — `_get_langchain_tools` wraps each MCP tool with `@tool`, calling `MCP_MANAGER.call_sync`.

## Discovery & sync

`services/skill_discovery.py` — `discover_agent_skills` GETs `/.well-known/skills`, `/skills`, `/agent/skills` from an agent endpoint; `sync_agent_skills` auto-creates `Skill` + `AgentSkill` rows. Triggered by `POST /api/agents/{id}/discover-skills` (owner/admin) or `POST /api/agent/discover-skills` (agent self).

## Security

- All MCP credentials (headers, env, OAuth tokens) are Fernet-encrypted at rest.
- Per-agent header overrides allow scoping credentials per agent even for shared platform servers.
- OAuth tokens (access/refresh) stored encrypted; refresh flow re-encrypts on each refresh.
- `visibility=platform` servers are admin-only to create — prevents users from injecting platform-wide MCP servers.
