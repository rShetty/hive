# HLD 07 — Deployment & CI/CD

Hive deploys as a single Dockerized FastAPI service (the marketplace/backend), with agents deployed as sibling containers, VPS docker-compose stacks, or local subprocesses. CI gates deploys on security scans.

## Docker images

| Image | Dockerfile | Purpose |
|-------|-----------|---------|
| `hive` (marketplace) | `/Dockerfile` | Backend + frontend + agent_app source. `python:3.11-slim`, installs `backend/requirements.txt + asyncpg`, copies `backend/`, `frontend/`, `docker/`, runs `uvicorn main:app --port 8080`. |
| `hive-agent:latest` (`AGENT_IMAGE`) | `docker/Dockerfile.agent` | Legacy "Hermes" managed agent. `python:3.11-slim`, inline deps (fastapi/uvicorn/httpx/python-dotenv), `EXPOSE 8000`, runs `main:app`. No crewai/langchain. |
| `openclaw/openclaw:latest` (`OPENCLAW_IMAGE`) | `docker/Dockerfile.openclaw` | OpenClaw agent. `python:3.11-slim`, inline deps (fastapi/uvicorn/httpx/pydantic), `EXPOSE 9000`, healthcheck on `/`, runs `main:app --port 9000`. No crewai/langchain (only the `openclaw` framework works in this image). |

**Note**: `docker/agent_app/requirements.txt` pins the "full" deps (incl. crewai/langchain/litellm) but is **not referenced by either agent Dockerfile** — it's the source of truth for the local-subprocess path where the Hive backend venv is reused. There's a version skew (0.115.6 in requirements.txt vs 0.109.0 in the Dockerfiles). See [LLD/04](../LLD/04-agent-runtime.md#dependency-notes).

## Compose

### `docker-compose.yml` (dev)
- Service `marketplace`: `build: .`, ports `8000:8000`, sqlite at `data/agent_marketplace.db`, mounts `./data:/data` and **docker.sock `/var/run/docker.sock:/var/run/docker.sock:ro`** (so Hive can spawn sibling agent containers — warned as a security risk), network `agent-marketplace`, `restart: unless-stopped`.
- Service `traefik` (opt-in via `profiles: [with-proxy]`): `traefik:v3.0`, docker provider, `:80`.
- **Known issue**: maps `8000:8000` but the image listens on `8080` (`Dockerfile` `ENV PORT=8080`) with no `PORT` override — likely broken as-is.

### `docker-compose.prod.yml` (prod)
- Service `marketplace`: `build: .`, ports `127.0.0.1:8080:8080` (loopback-only — traffic enters via nginx/Traefik), sqlite at `/app/data/agent_marketplace.db`, **requires** `ENCRYPTION_KEY` and `SECRET_KEY`, `MARKETPLACE_URL=https://hive.rajeev.me`, `HIVE_URL=http://localhost:8080`, `HIVE_DOMAIN=hive.rajeev.me`, `OPENCLAW_IMAGE`, docker.sock `:ro`, `restart: unless-stopped`.
- No Traefik service — production uses nginx + certbot provisioned per-agent by `openclaw_deployer.py`.

## Agent deploy paths (overview)

Four paths, detailed in [LLD/08](../LLD/08-deploy-paths.md):

1. **Legacy Hermes container** — `POST /api/agents/deploy` → `container_manager.create_container` runs `AGENT_IMAGE` on the `agent-marketplace` bridge, port `10000+` → `127.0.0.1`.
2. **OpenClaw one-click VPS** — `POST /api/agents/deploy-openclaw` → SSH to VPS, scp docker-compose + Dockerfile.openclaw + agent_app, write secrets as files, `docker compose build && up -d`, provision nginx subdomain `{slug}.{HIVE_DOMAIN}` + certbot.
3. **OpenClaw local Docker** — same endpoint, `OPENCLAW_DEPLOY_MODE=local` → `container_manager.create_openclaw_container` runs `OPENCLAW_IMAGE` with Traefik labels.
4. **BYOK hosted (local subprocess)** — `POST /api/agents/deploy-hosted` → `openclaw_local.spawn_openclaw_agent` launches `docker/agent_app/{main|main_langchain|main_crewai}.py` as a uvicorn subprocess on `127.0.0.1:<port>`. Rehydrated on Hive startup; watched by a 60 s watchdog.

## VPS nginx subdomains

`openclaw_deployer._provision_nginx_subdomain` (`services/openclaw_deployer.py:294`) writes an nginx server block for `{slug}.{HIVE_DOMAIN}` → `http://127.0.0.1:{hive_port}/a/{slug}/` — i.e. it proxies **through Hive's auth proxy**, so subdomain access still requires a Hive JWT. Certbot provisions SSL unless a wildcard cert is configured (`HIVE_SSL_CERT`/`HIVE_SSL_KEY`).

## CI/CD pipeline (`.github/workflows/ci.yml`)

Triggers: push to `main`, PR to `main`, nightly cron `0 0 * * *`, `workflow_dispatch`.

```
 ┌─────────────┐  ┌───────────────────┐  ┌─────────┐  ┌──────────────────┐
 │ secret-scan │  │ dependency-audit  │  │ codeql  │  │ hardening-check  │
 │ TruffleHog+ │  │ pip-audit +       │  │ py+js   │  │ hardcoded-secret │
 │ Gitleaks    │  │ npm audit         │  │         │  │ .env gitignored  │
 └──────┬──────┘  └─────────┬─────────┘  └─────────┘  │ SQLi patterns    │
        │                   │                        │ compose secrets  │
        └─────────┬─────────┘                        └────────┬─────────┘
                  │ (all three must pass)                      │
                  ▼                                            │
          ┌─────────────┐  (independent)                        │
          │   deploy    │ ◄─────────────────────────────────────┘
          │ SSH → VPS   │
          └─────────────┘
```

Jobs:
1. **secret-scan** — TruffleHog OSS (verified+unknown) + Gitleaks over full history.
2. **dependency-audit** — Python 3.12 `pip-audit -r backend/requirements.txt`; Node 22 `npm audit --omit=dev --audit-level=high` (both `|| true` — non-blocking).
3. **codeql** — matrix `[python, javascript]`, `security-extended`, autobuild + analyze.
4. **hardening-check** — hardcoded secret regex scan, `.env` gitignore check, SQL-injection pattern check (warning), docker-compose hardcoded-secret check.
5. **deploy** — `needs: [secret-scan, dependency-audit, hardening-check]` (not CodeQL, which runs independently). Runs on push-to-main or `workflow_dispatch`. Uses `appleboy/ssh-action` to SSH into `secrets.VPS_HOST`:
   - Clone/update `/opt/hive` from `github.com/rShetty/hive.git`, `git reset --hard origin/main`.
   - Write `.env` from GitHub secrets (encryption key, secret key, OpenRouter key, `HIVE_URL_OVERRIDE=https://hive.rajeev.me`, `ALLOWED_ORIGINS_OVERRIDE`, SSL paths, `COOKIE_SECURE=1`).
   - Build `docker/Dockerfile.agent`, then `docker compose -f docker-compose.prod.yml down && up -d --build --remove-orphans`.
   - Health-check: `sleep 10`, then 5 attempts polling `curl -sf http://localhost:8080/api/health` with 5 s backoff; on failure dumps `docker compose logs --tail=50` and exits 1.

## Port conventions

| Path | Container internal | Host |
|------|-------------------|------|
| Legacy Hermes (`Dockerfile.agent`) | 8000 | `10000+` (`BASE_PORT`) |
| OpenClaw local Docker (`Dockerfile.openclaw`) | 9000 | `9000+` (`OPENCLAW_PORT_START`) |
| OpenClaw VPS compose | 9000 | `9000+` |
| Local subprocess | 8080 (runtime default) | `9000+` (allocated) |
| Hive marketplace (prod) | 8080 | `127.0.0.1:8080` |

**Inconsistency**: `OPENCLAW_INTERNAL_PORT` is `8080` in `container_manager.py:197` but `9000` in `openclaw_deployer.py:16`. The VPS compose (`9000:9000`) matches its Dockerfile; the local Docker path maps `8080/tcp` against an image whose Dockerfile exposes `9000` — a mismatch worth fixing.

## VPS bootstrap (dev tooling, not production)

- `setup-script.sh` — VPS bootstrap: apt update, install python3/node 22/tmux/curl, install OpenCode + Omnigent, configure UFW for port 4096.
- `connect-vps.sh` — local SSH helper (hardcoded `187.127.140.125`, port 4096) with optional port-forwarding.
- `vps-setup-guide.md` — step-by-step VPS + MiMo model setup guide.

These are **developer tooling** for the OpenCode/Omnigent workflow on the VPS, not part of the Hive production deploy.
