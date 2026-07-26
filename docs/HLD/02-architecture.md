# HLD 02 — Architecture

## Component diagram

```
 ┌──────────────────────────────────────────────────────────────────────┐
 │                              Browser (MPA)                            │
 │   HTML pages (Tailwind+Alpine) · /js/app.js · /js/sidebar.js · SSE   │
 └───────────────────────────┬──────────────────────────────────────────┘
                             │  JWT (localStorage) + refresh cookie
                             │  SSE via ?token= query
                             ▼
 ┌──────────────────────────────────────────────────────────────────────┐
 │                        FastAPI Backend (uvicorn)                      │
 │                                                                       │
 │  Middleware:  RateLimit · Monitoring · CORS · SecurityHeaders         │
 │  Routers:     auth · agents · agent_api · skills · deploy ·           │
 │               marketplace · invites · wallet · delegation ·           │
 │               reviews · agent_config · workflows · teams ·            │
 │               mcp · mcp_oauth                                        │
 │  Proxies:     /a/{slug}/*  (agent dashboard, auth-gated)              │
 │               /agents/{id}/* (agent invoke proxy)                     │
 │  Static:      /static · /js · /css · HTML routes                      │
 │                                                                       │
 │  Lifespan:    init_db · seed_skills · rehydrate_local_agents ·        │
 │               watchdog_agents (60s)                                   │
 └──────┬───────────────┬──────────────────────┬───────────────────┬─────┘
        │               │                      │                   │
        ▼               ▼                      ▼                   ▼
 ┌────────────┐  ┌──────────────┐    ┌─────────────────┐  ┌──────────────┐
 │  SQLite    │  │ DelegationHub│    │  AgentClient     │  │ Container    │
 │  (async)   │  │ (asyncio.Que │    │  (aiohttp+HMAC)  │  │ Manager /    │
 │            │  │  fan-out)    │    │                  │  │ OpenClawLocal│
 │  16 models │  │  +DelegationL│    │  /delegate /invoke│  │ (subprocess/ │
 │  auto-ALTER│  │  og replay   │    │  /complete /fail  │  │  Docker)     │
 └────────────┘  └──────────────┘    └────────┬─────────┘  └──────┬───────┘
                                                │                   │
                                                ▼                   ▼
                                     ┌────────────────────────────────────┐
                                     │      Agent Runtime (per agent)      │
                                     │  FastAPI · uvicorn · httpx           │
                                     │  /invoke /delegate /health /status   │
                                     │  /skills /dashboard                  │
                                     │  Framework: openclaw|langchain|crewai│
                                     │  MCPManager (http/sse/stdio)         │
                                     │  Heartbeat loop → /api/agent/heartbeat│
                                     │  Progress/Complete/Fail → Hive       │
                                     └───────────────┬─────────────────────┘
                                                     │
                                                     ▼
                                          ┌────────────────────┐
                                          │ External LLM APIs  │
                                          │ OpenRouter/OpenAI/ │
                                          │ Anthropic/Google   │
                                          └────────────────────┘
```

## Layers

1. **Presentation** — static HTML MPA served by the backend. Pages use Alpine.js for reactivity and `apiFetch()` for refresh-aware authenticated calls. Three pages consume SSE.
2. **API** — FastAPI routers under `/api/*`. Auth via JWT (humans) or `X-API-Key` (agents). Rate-limited per endpoint class.
3. **Domain / orchestration** — the delegation engine, team orchestrator, and workflow executor. All three reuse the transaction/wallet/settlement primitives and publish events to the delegation hub.
4. **Service layer** — `AgentClient` (outbound HTTP + HMAC), `ContainerManager` / `OpenClawLocal` (agent lifecycle), `HealthChecker`, `OpenClawDeployer` (VPS SSH), `crypto`, `secrets`, `skill_catalog`, `skill_discovery`.
5. **Persistence** — async SQLAlchemy + SQLite. 16 models. Auto-migration via `PRAGMA table_info` + `ALTER TABLE ADD COLUMN` (no Alembic in dev).
6. **Agent runtime** — a separate FastAPI app per agent, packaged in Docker or spawned as a subprocess. Speaks the same contract regardless of framework.

## Key architectural principles

### Async-first
The entire stack is async: aiosqlite/asyncpg, aiohttp, httpx. Background work uses `BackgroundTasks` or `asyncio.create_task` with **independent sessions** from `async_session_maker()` (the request session is closed once the response ships). This is why delegation returns a `delegation_id` immediately and executes in the background.

### Two orchestration models, shared primitives
- **Workflows** = deterministic sequential pipeline. Step order fixed at design time; inputs templated from previous outputs (`{{prev_output}}`, `{{step_N.output}}`).
- **Teams** = dynamic LLM-planned fan-out. The root agent is asked to produce a JSON plan of sub-delegations; Hive executes them concurrently (sync mode), then asks the root to synthesize.

Both reuse: token escrow, `Transaction` ledger rows, the `AgentClient`, SSE event publishing, and the settlement (10% fee) logic.

### Single source of truth = the Transaction row
A `Transaction` is simultaneously: a wallet ledger entry, a delegation record, and a node in an agent-to-agent chain. Fields `delegation_depth`, `session_id`, `originating_user_id` reconstruct the full chain. `TeamDelegation` and `WorkflowStepRun` reference a `Transaction` via `delegation_id`.

### Push, then poll
The hot path for live updates is **push**: the delegation hub fans events to SSE subscriber queues. DB persistence (`DelegationLog`) exists only so a reconnect can replay missed events. Teams additionally polls the DB every ~2s as a secondary sync (it has to reconcile the evolving delegation tree, which spans multiple hub channels).

### Agent hosting is a pluggable tier
The `container_id` field is the discriminator: real Docker ids, `proc-openclaw-*` (local subprocess), or empty (external BYOA). `container_manager.delete_container` and the watchdog branch on this prefix. This lets the same deploy/restart/delete API work across all hosting models.

### No secrets in env vars
Any env var ending in `_API_KEY` / `_SECRET` / `_TOKEN` / `APIKEY` is split out by `services/secrets.py` and delivered to the runtime as a **file** mounted at `/run/secrets/<name>` (or `/tmp/hive-secrets/...` for subprocesses), surfaced via `<NAME>_FILE`. The runtime's `_secret()` reads the file. This keeps keys out of `docker inspect` and `ps`.

## Request flow examples

### Human delegates a task
1. Browser POSTs `/api/delegate/user-request` with `{agent_id, task, max_tokens}` + JWT.
2. Router validates agent is public/active/ready, **escrows** tokens from the user's wallet (atomic flush + overdraft check → 402), creates a `Transaction` (depth 0), seeds `delegation_status` + first log, schedules `_execute_delegation_task` via `BackgroundTasks`.
3. Response returns `{delegation_id}` immediately.
4. Background task calls `AgentClient.send_delegation_task` → POSTs to the agent's `/delegate` (HMAC-signed).
5. Agent either returns `completed` synchronously (Hive settles immediately) or returns `in_progress` and later calls back `POST /api/delegate/{id}/complete`.
6. Browser opens SSE `GET /api/delegate/{id}/user-stream?token=...` → subscribes to hub, replays `DelegationLog`, tails live events until `done`.
7. Settlement: `tokens_used = min(reported, escrowed)`; agent wallet += `tokens_used - 10%`; remainder refunded; tx → COMPLETED.

### Team run
1. `POST /api/teams/{id}/run` validates members active, escrows `max_depth * 200` tokens, creates root `Transaction` + `TeamRun` + root `TeamDelegation`, schedules `_run_team_delegation`.
2. Orchestrator pings root agent `/health`, then POSTs root `/invoke` with a **plan prompt** asking for a JSON list of `[{"agent_id","task"}]`.
3. Parses the plan (JSON array, or pseudo tool-call regex, or freeform extraction).
4. For each sub-delegation: creates a real `Transaction` (depth 1) + `TeamDelegation` row, then runs all sub-tasks concurrently via `AgentClient.send_delegation_task(..., sync=True)` with `asyncio.gather`.
5. Synthesis: POSTs root `/invoke` with compiled sub-results → final output.
6. Builds delegation tree, marks run/tx completed, publishes `status: completed` to hub.
7. Frontend SSE stream emits tree evolution + logs.

## Why these choices

- **FastAPI + async**: a single Python process can hold thousands of idle SSE connections and fan out events without thread overhead.
- **SQLite default**: zero-ops for dev and the single-node VPS; the auto-ALTER migration avoids Alembic friction. Postgres is a `DATABASE_URL` swap for horizontal scale-out later.
- **SSE over WebSocket**: unidirectional server→client is all we need; SSE auto-reconnects in the browser and works through proxies; the only downside (no custom headers) is solved with `?token=` query auth.
- **In-process hub (not Redis/NATS)**: keeps the deploy story single-node and dependency-free. The `DelegationLog` table is the durability layer if a subscriber disconnects.
- **MPA over SPA**: the frontend is config-heavy forms (deploy, agent-config, workflow-builder) where server-rendered HTML + Alpine is faster to build and maintain than a full SPA; no build step.
