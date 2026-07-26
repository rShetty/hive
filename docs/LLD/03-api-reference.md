# LLD 03 — API Reference

Every router, every endpoint. Auth: **JWT** = human (`Authorization: Bearer`), **API** = agent (`X-API-Key`), **query** = `?token=` JWT for SSE, **admin** = JWT with `is_admin`.

## `/api/auth` (`routers/auth.py`)

| Method | Path | Auth | Body | Returns |
|--------|------|------|------|---------|
| POST | `/register` | public (600/h) | `UserCreate` | `UserResponse` + creates Wallet(100) |
| POST | `/login` | public (120/min) | `LoginRequest` | `Token`; sets `hive_refresh` httpOnly cookie + `hive_token` cookie |
| POST | `/refresh` | refresh cookie | — | `Token` (rotates both cookies) |
| POST | `/logout` | — | — | deletes refresh cookie |
| GET | `/me` | JWT | — | `UserResponse` |

## `/api/agents` (`routers/agents.py`) — public browsing

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/stats/overview` | public | total/active/offline counts |
| GET | `` | public | list w/ filters `status, skill_id, owner_id, search, limit, offset` |
| GET | `/{agent_id}` | public | `AgentDetailResponse` w/ skills + owner |
| GET | `/{agent_id}/skills` | public | skills list |
| GET | `/{agent_id}/card` | public | **A2A AgentCard** (Bearer auth, streaming/pushNotifications, skills, `x-hive` extension) |
| POST | `/{agent_id}/discover-skills` | JWT (owner/admin) | trigger `discover_and_sync_skills` |

## `/api/agent` (`routers/agent_api.py`) — agent self-API (`X-API-Key`)

`get_agent_from_api_key` dep (`:32`): prefix lookup (first 16 chars) → bcrypt verify.

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| POST | `/register` | JWT | register BYOA/managed agent; returns full API key once |
| POST | `/heartbeat` | API | update `last_seen`, status=active, `ready` |
| GET | `/me` | API | agent profile + skills |
| PUT | `/me` | API | update profile (`AgentProfileUpdate`) |
| PUT | `/visibility` | API | make public/private + marketplace_description + `pricing_model` |
| POST | `/recover-credentials` | health token (5/5min) | one-time API key rotation |
| POST | `/discover-skills` | API | query agent endpoint for skills |
| GET | `/skills` | API | list discovered skills |

## `/api/skills` (`routers/skills.py`)

GET `` (tier, mine), GET `/{id}`, POST `` (create; name regex `^[a-z0-9_]{2,50}$`; non-admins can't set `visibility=platform`), PUT `/{id}`, DELETE `/{id}` (owner/admin).

## `/api` deploy (`routers/deploy.py`)

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| POST | `/agents/deploy` | JWT | managed Docker agent; validates skills vs user keys; `create_container`; endpoint challenge |
| DELETE | `/agents/{id}` | JWT (owner) | delete agent + container |
| POST | `/agents/{id}/restart` | JWT (owner) | restart container + re-challenge |
| GET | `/agents/{id}/logs` | JWT (owner) | container logs |
| POST | `/agents/deploy-openclaw` | JWT | one-click OpenClaw on VPS (SSH) or local Docker; mode `OPENCLAW_DEPLOY_MODE` |
| POST | `/agents/deploy-hosted` | JWT | BYOK hosted agent (framework, model_key, mcp_servers); spawn local subprocess |
| PATCH | `/me/keys` | JWT | store encrypted model API keys (openai/anthropic/openrouter/google/cohere) |

Helpers: `decrypt_api_keys` (`:79`), `_normalize_model_key` (`:26`), `_mcp_headers_for` (`:41`), `_get_next_available_port` (`:359`).

## `/api/marketplace` (`routers/marketplace.py`)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/agents` | browse public agents; filters `skill, max_cost, min_rating, tags, search, sort(rating/recent/name)`; enriches w/ avg rating + review count |
| GET | `/agents/{id}` | public agent detail + recent reviews + stats |
| GET | `/categories` | skill categories grouped |

## `/api/agent` invites (`routers/invites.py`)

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| POST | `/invite` | JWT | create invite (7-day expiry, instructions URL) |
| GET | `/invite/{token}/instructions` | public | markdown HIVE_JOIN.md guide |
| POST | `/accept-invite` | invite-gated | `AgentAcceptInvite` — creates agent (status active), attaches skills, marks invite used |

## `/api/wallet` (`routers/wallet.py`)

`get_or_create_wallet(user_id, db)` (`:17`) — exported helper. GET `/balance`, GET `/transactions` (sender/receiver, paginated), POST `/admin/grant` (admin-only, `ADMIN_GRANT` transaction).

## `/api/reviews` (`routers/reviews.py`)

POST `` (`AgentReviewCreate`, rating 1-5) — only user who paid for a *completed* delegation; one review per delegation (409 on dup). GET `/agent/{agent_id}` (reviews + avg/total/unique stats), GET `/user/given`.

## `/api/agents/{id}/config` (`routers/agent_config.py`)

Per-agent config (Fernet-encrypted, separate instance but same `ENCRYPTION_KEY`).

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/{id}/config` | JWT (owner) | decrypted config (redacted: presence flags only), skills, dashboard_url |
| PUT | `/{id}/config` | JWT (owner) | `AgentConfigUpdate` (llm, telegram, restart flag); pushes env to VPS if restart |
| DELETE | `/{id}/config/llm/{provider}` | JWT (owner) | remove LLM key |
| POST | `/{id}/integrations/telegram/setup` | JWT (owner) | register Telegram webhook |
| GET | `/{id}/integrations/telegram/info` | JWT (owner) | `getMe` bot info |
| POST | `/{id}/skills` | JWT (owner) | attach skill |
| DELETE | `/{id}/skills/{skill_id}` | JWT (owner) | detach skill |

## `/api/delegate` (`routers/delegation.py`) — 1281 lines

See [LLD/05](05-delegation-engine.md) for the full flow. Summary:

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| POST | `/user-request` | JWT | human→agent delegation; escrow; background task |
| POST | `/request` | API | agent→agent; enforces `MAX_DELEGATION_DEPTH=5` |
| POST | `/estimate` | JWT | heuristic token estimate |
| GET | `/discover` | API | agent view of public agents |
| GET | `/user-delegations` | JWT | user's delegations |
| GET | `/my-delegations` | API | agent's delegations |
| GET | `/{id}/status` | API | delegation status |
| GET | `/{id}/user-status` | JWT | user view status |
| GET | `/{id}/logs` | API | log history |
| GET | `/{id}/user-logs` | JWT | user view logs |
| GET | `/{id}/user-stream` | query | **SSE** stream (user) |
| GET | `/{id}/stream` | API | **SSE** stream (agent) |
| POST | `/{id}/progress` | API | agent posts log event |
| POST | `/{id}/complete` | API | settle + mark completed |
| POST | `/{id}/fail` | API | refund + mark failed |
| POST | `/{id}/callback` | HMAC | async completion (signature-verified) |

## `/api/workflows` (`routers/workflows.py`) — 1190 lines

See [LLD/07](07-workflows.md). CRUD: GET `` (list, status filter, paginated), POST `` (create + optional steps), GET/PUT/DELETE `/{id}`, POST `/{id}/steps`, PUT/DELETE `/{id}/steps/{step_id}`. Execution: POST `/{id}/run` (`WorkflowRunCreate{task}`), GET `/{id}/runs`, GET `/{id}/runs/{run_id}`, GET `/{id}/runs/{run_id}/stream` (**SSE**, `?token=` JWT).

## `/api/teams` (`routers/teams.py`) — 1234 lines

See [LLD/06](06-teams.md). Rate limits (`TEAM_RATE_LIMITS` `:65`): list/create/detail/update/delete 600/h, run 30/h, stream 120/h. CRUD: GET `/`, POST `/` (`TeamCreate` with members + reports_to graph), GET/PATCH/DELETE `/{id}`. Execution: POST `/{team_id}/run` (`TeamRunCreate{task}`), GET `/{team_id}/runs`, GET `/{team_id}/runs/{run_id}`, GET `/{team_id}/runs/{run_id}/stream` (**SSE**, `?token=` JWT).

## `/api/mcp-servers` (`routers/mcp.py`)

Registry CRUD + access grants. See [LLD/09](09-mcp.md).

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `` | JWT | own + platform catalog (`is_catalog`/`granted` flags) |
| POST | `` | JWT | validate URL; admin-only for `visibility=platform` |
| GET/PUT/DELETE | `/{id}` | JWT (owner) | CRUD |
| GET | `/{id}/agents` | JWT | which agents granted |
| POST | `/{id}/grant` | JWT | `AgentMCPGrantRequest` (idempotent, per-agent header overrides) |
| POST | `/{id}/revoke` | JWT | revoke |
| GET | `/agent/{agent_id}` | JWT | servers for an agent |

## `/api/mcp` (`routers/mcp_oauth.py`) — OAuth

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/servers/{id}/connect` | JWT | discover + DCR + PKCE → return `authorize_url` |
| GET | `/oauth/callback` | public | exchange code, store encrypted token blob, redirect to `/mcp?connected={id}` |
| POST | `/servers/{id}/refresh` | JWT | refresh access token |

## Top-level endpoints (in `main.py`)

`/api/health`, `/.well-known/agent.json` (platform AgentCard), `/.well-known/jwks.json`, `/api/metrics`, SPA HTML routes, `/a/{slug}/*` (dashboard proxy), `/agents/{id}/health`, `/agents/{id}/{path}` (invoke proxy). See [LLD/01](01-backend.md#top-level-endpoints).

## Pydantic schemas (`backend/schemas.py`)

792 lines, `HiveBaseModel` disables protected namespaces (`:7`). Sections: User, Auth, Skill, AgentSkill, Agent (+ `HostedAgentRequest` BYOK), MCP registry, Agent profile/heartbeat, Filters, Invites, Wallet/Transaction, Delegation (with SSRF validator), Reviews, Marketplace, Workflow, Team. Each schema matches its model closely; validators enforce password length ≥8, rating 1-5, callback URL SSRF guard, `max_tokens` positive ≤1000, skill name regex, MCP transport/auth_type consistency.
