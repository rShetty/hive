# LLD 01 — Backend Core

The FastAPI app entrypoint is `backend/main.py` (680 lines). This document covers app construction, lifespan, middleware, static/frontend serving, and the two HTTP proxies.

## App construction

```python
app = FastAPI(title="Hive 🐝", version="1.0.0", lifespan=lifespan)  # main.py:67-72
```

Env loaded via `python-dotenv` from `../.env` with `override=False` (`main.py:10-15`).

## Lifespan (`main.py:30-64`)

**Startup:**
1. `init_db()` — `Base.metadata.create_all` (creates missing tables) + `_add_missing_columns` (auto-ALTER for new columns; see [LLD/02](02-database.md#auto-migration)).
2. `seed_skills(session)` — insert the default skill catalog (`services/skill_catalog.py`).
3. `rehydrate_local_agents(session)` — respawn any `proc-openclaw-*` agents that died on restart (fresh API key, encrypted config re-read).
4. `asyncio.create_task(watchdog_agents())` — every 60 s restart dead subprocess agents.

**Shutdown:** `cleanup_all()` stops all spawned subprocesses (`main.py:60-64`).

## Middleware (registration order = reverse execution)

| Middleware | File | Purpose |
|-----------|------|---------|
| `RateLimitExceeded` handler | `middleware/rate_limit.py` | JSON 429 response |
| `MonitoringMiddleware` | `middleware/monitoring.py` | Logs req/resp, sets `X-Process-Time`, collects metrics |
| `CORSMiddleware` | (Starlette) | Origins from `ALLOWED_ORIGINS` (default `http://localhost:8000`) |
| `SecurityHeadersMiddleware` | `main.py:99-110` | `X-Content-Type-Options`, `X-Frame-Options`, `X-XSS-Protection`, `Referrer-Policy`, `Permissions-Policy` |

`app.state.limiter = limiter` is set first (`main.py:75`). See [LLD/12](12-config-env.md) for rate limit values.

## Routers included (`main.py:113-127`)

auth, agents, agent_api, skills, mcp, mcp_oauth, deploy, marketplace, invites, wallet, delegation, reviews, agent_config, workflows, teams. Prefixes documented in [LLD/03](03-api-reference.md).

## Top-level endpoints (`main.py`)

| Method | Path | Purpose | Line |
|--------|------|---------|------|
| GET | `/api/health` | health check | 130 |
| GET | `/.well-known/agent.json` | platform A2A AgentCard | 136 |
| GET | `/.well-known/jwks.json` | JWKS (empty — HS256 symmetric, documented) | 189 |
| GET | `/api/metrics` | metrics summary (`Metrics.get_summary()`) | 210 |
| GET | `/` | serve `index.html` (or JSON pointer to `/docs`) | 246 |
| GET | `/agents`, `/agents/{id}`, `/agent-detail`, `/login`, `/signup`, `/deploy`, `/settings`, `/tasks`, `/workflows`, `/workflows/new`, `/workflows/{id}`, `/teams`, `/teams/{id}`, `/agent-config`, `/skills`, `/mcp` | SPA routes → `_serve_frontend` | 257-344 |
| GET | `/delegate` | legacy → 302 redirect to `/tasks` | 322 |
| ANY | `/a/{slug}` and `/a/{slug}/{path:path}` | **agent dashboard proxy** (auth-gated) | 438-548 |
| GET | `/agents/{agent_id}/health` | health probe of managed agent | 551-605 |
| ANY | `/agents/{agent_id}/{path:path}` | **proxy to agent container** (`/invoke`, etc.) | 609-673 |

## Static mounts (`main.py:216-233`)

- `/static` → whole `frontend/` dir.
- `/js` → `frontend/js/`.
- `/css` → `frontend/css/`.

`frontend_path` resolves to `../frontend` (dev) or `/app/frontend` (Docker). `_serve_frontend(filename)` (`main.py:236-243`) returns `FileResponse` or 404.

## Agent dashboard proxy (`/a/{slug}/`) — `main.py:438-548`

Serves each OpenClaw agent's built-in UI behind a Hive JWT check.

1. **Auth** — JWT from `hive_token` cookie or `Authorization: Bearer`. Missing → returns inline HTML login page (`_LOGIN_PAGE`, `main.py:351-407`). Validation in `_validate_hive_token` (`main.py:410-435`).
2. **Slug validation** — `re.fullmatch(r"[a-z0-9][a-z0-9\-]{0,119}", slug)` (`main.py:469`) — path-traversal guard.
3. **Resolve agent** by slug; proxy to `http://127.0.0.1:{internal_port}/{safe_path}` via `aiohttp` (30 s timeout, `allow_redirects=False`).
4. **Header stripping** — removes `authorization`, `cookie`, `host`, `x-hive-*`, `x-forwarded-*` from request; `server`, `x-powered-by` from response.
5. **Injection** — adds `X-Hive-User-Id`, `X-Hive-Agent-Slug`.
6. **Failure** → 502 "Agent unreachable".

## Agent invoke proxy — `main.py:609-673`

Generic proxy to agent container `http://localhost:{internal_port}/{path}`. Allows internal delegation calls (`User-Agent: Hive-Marketplace`) even when agent status is offline. Parses JSON body else `{"raw": ...}`.

## Health probe — `main.py:551-605`

`GET /agents/{agent_id}/health` — proxies to the agent's `/health?token=...` (using the agent's `health_check_token` or by port). Used by the endpoint challenge and team run pre-flight (`_check_agent_alive`).

## Metrics (`middleware/monitoring.py`)

`Metrics` class holds in-memory counters: `requests_total`, by-status, by-endpoint, `delegation_count`/`success`/`failed`, `tokens_transferred`, `agents_registered`, `users_registered`. Exposed via `metrics.get_summary()` at `/api/metrics`. `log_event` is a structured logger.

## Run (`main.py:676-680`)

```python
uvicorn main:app --host 0.0.0.0 --port $PORT --reload
```

`PORT` default 8080 (env override; note the dev `docker-compose.yml` maps 8000:8000 without overriding `PORT` — see [HLD/07](../HLD/07-deployment.md#compose)).
