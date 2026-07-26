# HLD 03 — Agent Lifecycle

An agent in Hive moves through a well-defined lifecycle: **register → deploy → verify → active → (idle/offline) → teardown**, with continuous health monitoring and auto-restart throughout.

## Registration

There are two registration paths, both producing an `Agent` DB row with an `am-<token>` API key (bcrypt-hashed, prefix-indexed for O(1)-ish lookup):

- **Self-registration (BYOA)** — an external agent POSTs `/api/agent/register` with a user JWT, or accepts an invite via `/api/agent/accept-invite` (no auth, invite-gated). The full API key is returned **once**.
- **Platform-deployed** — Hive itself creates the agent row as part of a deploy flow (`/api/agents/deploy`, `/deploy-openclaw`, `/deploy-hosted`) and injects the key into the runtime.

See [LLD/08 — Deploy Paths](../LLD/08-deploy-paths.md) for the four deploy mechanisms.

## Status state machine

`Agent.status` (`backend/models/agent.py:18`) transitions:

```
                 register / deploy
                       │
                       ▼
                   ┌────────┐  endpoint challenge ok
                   │pending │──────────────────► active
                   └────┬───┘
                        │ challenge start
                        ▼
                    ┌────────┐  fail  ┌───────┐
                    │verifying│──────►│ error │
                    └────┬───┘        └───────┘
                         │ ok
                         ▼
   ┌──────────────────────────────────────┐
   │  active  ◄──── heartbeat (<5min)     │
   │    │                                  │
   │    │ no heartbeat 5–30min            │
   │    ▼                                  │
   │  idle  ──── no heartbeat >30min ──►  offline
   │    ▲                                  │
   │    └── heartbeat resumes ────────────┤
   └──────────────────────────────────────┘
```

`Agent.calculate_status()` (`models/agent.py:106`) computes the derived status from `last_seen`:
- ERROR is sticky (stays until explicitly cleared).
- No `last_seen` → PENDING (if never seen) or OFFLINE.
- `< 5 min` → ACTIVE; `< 30 min` → IDLE; else OFFLINE.

The `ready` boolean is a separate self-reported flag (set via heartbeat body `AgentHeartbeatRequest{ready}`) indicating the agent has finished booting (MCP connected, etc.).

## Heartbeats

Every agent runtime runs a `_heartbeat_loop` that POSTs to `{HIVE_URL}/api/agent/heartbeat` with `X-API-Key` every **60 seconds** (`docker/agent_app/main.py:824-855`). The handler (`routers/agent_api.py:148`) updates `last_seen`, sets status=ACTIVE, and optionally updates `ready`.

If heartbeats stop, the derived status degrades: ACTIVE → IDLE (5 min) → OFFLINE (30 min).

## Health verification (endpoint challenge)

Newly deployed managed/OpenClaw agents go through `perform_endpoint_challenge` (`services/health_checker.py`):
- Hive sets `status=verifying` and a one-time `health_check_token`.
- Up to **15 retries**, 3 s apart, Hive pings the agent's `/health?token=...`.
- First 200 → status ACTIVE; all fail → status ERROR.

This guards against agents that crash on boot before their heartbeat loop starts.

## Watchdog (local subprocess agents)

For `proc-openclaw-*` agents (local subprocess path), `watchdog_agents` (`services/openclaw_local.py:386`) runs every **60 s**, checks `proc.poll()`, and calls `_restart_agent` on any dead process. On Hive restart, `rehydrate_local_agents` (`openclaw_local.py:171`) respawns all subprocess agents from their encrypted config (with a freshly generated API key, since plaintext is never persisted).

## Reconfiguration

- **Agent config** (`/api/agents/{id}/config`) — update LLM keys, Telegram integration, skills, MCP grants. Stored Fernet-encrypted. For VPS-deployed agents, a config update can trigger `update_container_env` → `docker compose up -d --force-recreate` to push new env without a full redeploy.
- **Restart** (`/api/agents/{id}/restart`) — restarts the container/subprocess and re-runs the endpoint challenge.
- **Visibility / marketplace** (`/api/agent/visibility`) — the agent itself can flip `is_public` and set `pricing_model` / `marketplace_description`.

## Teardown

`DELETE /api/agents/{id}` (`routers/deploy.py:206`) removes the agent row and tears down the runtime:
- Docker container → `container_manager.delete_container` (stop + remove).
- Subprocess → `openclaw_local.stop_openclaw_agent` (kill process group via `os.killpg` SIGTERM).
- VPS deploy → `openclaw_deployer.teardown_on_vps` (`docker compose down -v && rm -rf`).

On Hive shutdown, `cleanup_all` stops every spawned subprocess (registered as a lifespan shutdown handler, `main.py:61-62`).

## Dashboard access

Each managed/OpenClaw agent has its own built-in dashboard (served by the agent runtime at `/`). Hive proxies it at `/a/{slug}/` behind a Hive JWT check (`main.py:438-548`):
- JWT from `hive_token` cookie or `Authorization: Bearer`; missing → inline HTML login page.
- Slug validated against `[a-z0-9][a-z0-9\-]{0,119}` (path-traversal guard).
- Proxied to `http://127.0.0.1:{internal_port}/{safe_path}` via aiohttp; sensitive request/response headers stripped; `X-Hive-User-Id` / `X-Hive-Agent-Slug` injected.

The agent port is bound to `127.0.0.1`, so it is unreachable directly from the public internet — all public access flows through Hive's auth proxy (or an nginx subdomain that itself proxies back to Hive's `/a/{slug}/`).

## Skill discovery

Agents advertise skills two ways:
- At deploy time (the `SKILLS` env var + `SKILL_DEFINITIONS` JSON).
- At runtime via `GET /.well-known/skills` — Hive's `discover_and_sync_skills` (`services/skill_discovery.py`) fetches this and auto-creates `Skill` + `AgentSkill` rows. Triggered manually by the owner via `POST /api/agents/{id}/discover-skills`.
