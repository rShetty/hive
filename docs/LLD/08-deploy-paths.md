# LLD 08 — Deploy Paths

Hive has **four distinct agent deploy paths**, all funneling into the same agent runtime contract (see [LLD/04](04-agent-runtime.md)). The `container_id` field is the discriminator.

## Path A — Legacy "Hermes" container deploy

**Endpoint**: `POST /api/agents/deploy` (`routers/deploy.py:90-196`)

1. Validate skills against the user's decrypted model keys (`deploy.py:101-111`).
2. Create an `Agent` DB row with a freshly generated `am-...` API key (hashed, `deploy.py:129-150`).
3. `container_manager.create_container(...)` (`services/container_manager.py:60-113`):
   - Runs the `AGENT_IMAGE` image (default `hive-agent:latest`, built from `docker/Dockerfile.agent`) via `docker.from_env()`.
   - Network: `agent-marketplace` bridge.
   - Port: container `8000/tcp` → `127.0.0.1:<port>` (port allocated from `BASE_PORT=10000` upward, bind-probe).
   - `restart_policy=unless-stopped`.
   - Labels: `hive/agent-id`, `hive/agent-name`, `hive/managed`.
   - Env: `AGENT_ID`, `AGENT_NAME`, `AGENT_API_KEY`, `MARKETPLACE_URL`, `SKILLS`, `<PROVIDER>_API_KEY` per user key.
4. Agent status → `VERIFYING`; background **endpoint challenge** runs (`deploy.py:182-203`, `health_checker.perform_endpoint_challenge`).

## Path B — OpenClaw one-click VPS deploy

**Endpoint**: `POST /api/agents/deploy-openclaw` (`routers/deploy.py:390-607`)

Requires `OPENCLAW_VPS_HOST` + `OPENCLAW_VPS_SSH_KEY_PATH` env (`deploy.py:406-422`).

1. Generate `instance_id`, API key, slug.
2. Pick a free port (`_get_next_available_port`, `deploy.py:359-379`, scanning from `OPENCLAW_PORT_START=9000`).
3. Resolve default skill set (`terminal, file_ops, web_extract, planning, code_review`, `deploy.py:321-327`) + extras.
4. Mode selected by `OPENCLAW_DEPLOY_MODE` env: `local` | `vps` | `auto` (default; vps if configured, else local) (`deploy.py:462-469`).

### VPS mode (`deploy.py:471-492`)
- `openclaw_deployer.generate_compose(...)` (`services/openclaw_deployer.py:28-105`):
  - Emits docker-compose.yml for service `openclaw-{instance_id[:8]}`.
  - `build: .` unless a custom `OPENCLAW_IMAGE` is set (`:69-72`).
  - Secrets (any key ending in `_API_KEY`/`_SECRET`/`_TOKEN`/`APIKEY`, `secrets.py:11`) become Docker `secrets:` entries (`:58-65,96-104`) mounted at `/run/secrets/<name>:ro`, surfaced via `<NAME>_FILE` env.
  - Network: external `hive-net`.
- `deploy_to_vps` (`openclaw_deployer.py:108-291`):
  - SSH (`StrictHostKeyChecking=no`) to VPS.
  - `mkdir -p /opt/hive/openclaw-{id[:8]}`.
  - scp compose as `docker-compose.yml`.
  - scp `docker/Dockerfile.openclaw` + `docker/agent_app/` to remote dir; rename `Dockerfile.openclaw` → `Dockerfile` so `docker build .` works (`:184-209`).
  - Write each secret value to `./secrets/<name>` via heredoc, `chmod -R 600` (`:211-250`).
  - `cd remote_dir && docker compose build && docker compose up -d` (`:252-264`).
  - If `HIVE_DOMAIN` + slug set: provision nginx server block for `{slug}.{HIVE_DOMAIN}` → `http://127.0.0.1:{hive_port}/a/{slug}/` (proxies through Hive for auth) + certbot unless wildcard cert (`_provision_nginx_subdomain`, `:294-406`).
- On success: `config_encrypted` stores API key, status → `ACTIVE`, response includes `registration_prompt` (`deploy.py:555-600`).

### Reconfigure without redeploy
`update_container_env` (`openclaw_deployer.py:439-507`) regenerates compose, runs `docker compose up -d --force-recreate`.

### Teardown
`teardown_on_vps` runs `docker compose down -v && rm -rf` (`:409-436`).

## Path C — OpenClaw local Docker

Same endpoint, `OPENCLAW_DEPLOY_MODE=local` (or auto with no VPS configured).

- `container_manager.create_openclaw_container(...)` (`container_manager.py:200-306`):
  - Runs `OPENCLAW_IMAGE` (default `openclaw/openclaw:latest`) with Docker SDK.
  - Port `8080/tcp` → `127.0.0.1:<port>`.
  - Secret files mounted as bind mounts at `/run/secrets/<name>:ro` (`:257-273`).
  - Traefik labels for `{slug}.{hive_domain}` routing when a domain is set (`:283-290`).
- Falls back to spawning a local subprocess (Path D) if Docker unavailable.

**Port mismatch**: `OPENCLAW_INTERNAL_PORT` is `8080` in `container_manager.py:197` but the `Dockerfile.openclaw` exposes/listens on `9000`. The local Docker path maps `8080/tcp` against an image listening on `9000` — a mismatch worth fixing.

## Path D — BYOK hosted (local subprocess)

**Endpoint**: `POST /api/agents/deploy-hosted` (`routers/deploy.py:624-802`)

Bring-your-own-key: user supplies a framework (`openclaw`/`langchain`/`crewai`), a model key, optional MCP servers, and selected skills.

1. Create `AgentType.MANAGED` row.
2. Persist `{framework, model_key, mcp_servers}` encrypted (`deploy.py:685-689,733-738`).
3. Resolve MCP servers from registry, create `AgentMCPAccess` grants (`deploy.py:696-729`).
4. Build env vars (`SKILLS`, `SKILL_DEFINITIONS`, `<PROVIDER>_API_KEY` via `_KEY_ENV_MAP`, `MCP_SERVERS` JSON, `deploy.py:742-762`).
5. `openclaw_local.spawn_openclaw_agent(..., framework=req.framework)` (`deploy.py:766-775`).
6. Dashboard served at `/a/{slug}/` (`deploy.py:793`).

### `spawn_openclaw_agent` (`services/openclaw_local.py:46-168`)
- Launches `docker/agent_app/{main|main_langchain|main_crewai}.py` as a `uvicorn` **subprocess** on `127.0.0.1:<port>` (`:155-163`).
- Reuses the Hive backend's Python interpreter (which has fastapi/uvicorn/httpx; crewai/langchain only if installed).
- Module selected by `_MODULE_MAP` (`:67-72`).
- Secrets written to `/tmp/hive-secrets/proc-{id[:8]}/<name>` (mode 0600) and exposed via `<NAME>_FILE` (`:120-131`).
- `HIVE_URL` forced to `localhost` (preserving port) so callbacks hit the local Hive, not the public URL (`:77-94`).
- Returns synthetic container id `proc-openclaw-<id[:8]>` (`:168`).

### Lifecycle / resilience
- **Rehydration on Hive startup** — `rehydrate_local_agents` (`openclaw_local.py:171-281`), called from `main.py:44-46`. Finds every agent whose `container_id` starts with `proc-openclaw-`, decrypts `config_encrypted`, regenerates a fresh API key (plaintext never persisted), re-spawns.
- **Watchdog** — `watchdog_agents` (`openclaw_local.py:386-413`), started as background task (`main.py:50`). Every 60 s checks `proc.poll()`; calls `_restart_agent` (`:312-383`) on dead processes.
- **Shutdown** — `cleanup_all` (`openclaw_local.py:304-309`) stops every spawned subprocess; registered as shutdown handler (`main.py:61-62`).
- `stop_openclaw_agent` (`openclaw_local.py:284-301`) kills the whole process group via `os.killpg(..., SIGTERM)`.
- `container_manager.delete_container` (`container_manager.py:142-168`) special-cases `proc-openclaw-` ids to call `stop_openclaw_agent`.

## BYOA (external) — invite flow

External agents register themselves via `/api/agent/register` (JWT) or `/api/agent/accept-invite` (invite-gated). No container is created; the agent provides its own `endpoint_url` and heartbeats. `container_id` is empty. See [HLD/03](../HLD/03-agent-lifecycle.md#registration).

## Container manager constants (`services/container_manager.py`)

- `NETWORK_NAME = "agent-marketplace"`
- `BASE_PORT = 10000`
- `MAX_AGENTS = 100`
- `AGENT_IMAGE` (default `hive-agent:latest`)
- `OPENCLAW_IMAGE` (default `openclaw/openclaw:latest`)
- `OPENCLAW_INTERNAL_PORT` (8080 local / 9000 VPS — inconsistent, see above)

Falls back to mock / local subprocess when Docker unavailable.

## Port conventions summary

| Path | Container internal | Host | Notes |
|------|-------------------|------|-------|
| A (Hermes) | 8000 | `10000+` | `Dockerfile.agent` `EXPOSE 8000` |
| B (OpenClaw VPS) | 9000 | `9000+` | `Dockerfile.openclaw` `EXPOSE 9000` |
| C (OpenClaw local Docker) | 8080 (configured) | `9000+` | **mismatch** with image's 9000 |
| D (local subprocess) | runtime default | `9000+` (allocated) | reuses backend venv |
| Hive marketplace (prod) | 8080 | `127.0.0.1:8080` | `Dockerfile` `ENV PORT=8080` |
