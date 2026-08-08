# LLD 12 — Configuration & Environment

Every environment variable Hive consumes, its default, and where it's read.

## Core / app

| Var | Default | Where | Purpose |
|-----|---------|-------|---------|
| `DATABASE_URL` | `sqlite+aiosqlite:///./agent_marketplace.db` | `database.py:8` | DB connection. Postgres: `postgresql+asyncpg://...` |
| `PORT` | `8080` | `main.py:679` | uvicorn listen port |
| `MARKETPLACE_URL` | `http://localhost:8000` | many files | public base URL; used to resolve relative agent endpoints |
| `HIVE_URL` | `http://localhost:8080` | `deploy.py:348`, `openclaw_deployer.py:18`, `openclaw_local.py` | URL agents use to call back to Hive |
| `ALLOWED_ORIGINS` | `http://localhost:8000` (comma-sep) | `main.py:84` | CORS origins |
| `DEV_MODE` | unset | `auth.py:16` | if set, allows insecure `SECRET_KEY` default for dev |
| `HOST_IP` | `127.0.0.1` | `deploy.py:544` | host IP for container port binding |

## Security / crypto

| Var | Default | Where | Purpose |
|-----|---------|-------|---------|
| `SECRET_KEY` | (required; insecure default only if `DEV_MODE=1`) | `auth.py:18` | JWT HS256 signing |
| `ENCRYPTION_KEY` | (ephemeral if unset) | `crypto.py:8`, `deploy.py:59`, `agent_config.py:31` | Fernet at-rest encryption. **Required in prod** (`docker-compose.prod.yml`) |
| `HIVE_SIGNING_SECRET` | `change-me-in-production` | `config.py:16`, `delegation.py:77`, `agent_client.py:27` | HMAC for delegation payloads. **Must override in prod** (enforced by `config.enforce_prod_config`). Superseded by per-agent Ed25519 keys but retained for dual-signing transition. |
| `REDIS_URL` | (required in prod) | `config.py:20`, `kvstore.py:18`, `rate_limit.py:10` | Redis for JWT denylist, distributed rate limits, callback replay-nonce store. In-memory fallback in dev only. |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `15` | `auth.py:38` | JWT access token lifetime |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `30` | `auth.py:39` | refresh token lifetime |
| `COOKIE_SECURE` | dev-False / prod-True | `auth.py:42` | Secure flag on cookies |

## Agent / OpenClaw deploy

| Var | Default | Where | Purpose |
|-----|---------|-------|---------|
| `OPENCLAW_DEPLOY_MODE` | `auto` (vps if configured, else local) | `deploy.py:462` | deploy mode: `auto`/`local`/`vps` |
| `OPENCLAW_VPS_HOST` | — | `deploy.py:343` | VPS SSH host |
| `OPENCLAW_VPS_SSH_KEY_PATH` | — | `deploy.py:344` | SSH key path |
| `OPENCLAW_VPS_SSH_USER` | `root` | `deploy.py:345` | SSH user |
| `OPENCLAW_VPS_SSH_PORT` | `22` | `deploy.py:346` | SSH port |
| `OPENCLAW_PORT_START` | `9000` | `deploy.py:347` | host port allocation start |
| `OPENCLAW_IMAGE` | `openclaw/openclaw:latest` | `container_manager.py:196`, `openclaw_deployer.py:15` | OpenClaw Docker image |
| `OPENCLAW_INTERNAL_PORT` | `8080` (container_manager) / `9000` (deployer) | `container_manager.py:197`, `openclaw_deployer.py:16` | container listen port (**inconsistent — see LLD/08**) |
| `OPENCLAW_MOCK_MODE` | unset | `openclaw_deployer.py:17` | mock deploy (no real container) |
| `OPENCLAW_PYTHON` | `sys.executable` | `openclaw_local.py:36` | Python interpreter for subprocess agents |
| `OPENCLAW_LOCAL_HIVE_URL` | — | `openclaw_local.py:85` | override Hive URL for local subprocess agents |
| `AGENT_IMAGE` | `hive-agent:latest` | `container_manager.py:25` | legacy Hermes agent image |
| `HIVE_DOMAIN` | — | `openclaw_deployer.py:21` | base domain for nginx subdomains |
| `HIVE_SSL_CERT` | — | `openclaw_deployer.py:24` | wildcard SSL cert path |
| `HIVE_SSL_KEY` | — | `openclaw_deployer.py:25` | wildcard SSL key path |

## LLM keys (server-level fallbacks)

| Var | Default | Where | Purpose |
|-----|---------|-------|---------|
| `OPENROUTER_API_KEY` | — | `openclaw_local.py:102` | OpenRouter provider key (forwarded to subprocess agents) |
| `OPENROUTER_MODEL` | `openai/gpt-4o-mini` | `openclaw_local.py:115` | default model |
| `OPENAI_API_KEY` | — | `deploy.py:509`, `agent_config.py:139` | OpenAI provider |
| `ANTHROPIC_API_KEY` | — | same | Anthropic provider |
| `GOOGLE_API_KEY` | — | same | Google provider |
| `COHERE_API_KEY` | — | same | Cohere provider |

Per-user keys are stored Fernet-encrypted in `User.model_api_keys_encrypted` and injected per-agent at deploy time.

## Delegation tuning

| Var / constant | Default | Where | Purpose |
|----------------|---------|-------|---------|
| `AGENT_DELEGATION_TIMEOUT` | `300` | `teams.py:58` | delegation timeout (seconds) |
| `HIVE_API_KEY` | — | `teams.py:60` | API key for Hive's own outbound calls |
| `MAX_DELEGATION_DEPTH` | `5` (constant) | `delegation.py:73` | max agent-to-agent chain depth |
| `PLATFORM_FEE_PCT` | `0.10` (constant) | `delegation.py:70` | 10% platform fee on settlement |

## Docker / network

| Var / constant | Default | Where | Purpose |
|----------------|---------|-------|---------|
| `NETWORK_NAME` | `agent-marketplace` (constant) | `container_manager.py:22` | Docker bridge network for agents |
| `BASE_PORT` | `10000` (constant) | `container_manager.py:23` | host port allocation start for legacy agents |
| `MAX_AGENTS` | `100` (constant) | `container_manager.py:24` | max managed agents |

## Rate limits (`middleware/rate_limit.py`)

`slowapi.Limiter`, fixed-window. Storage is Redis (`REDIS_URL`) in production; in-memory fallback in dev. `default_limits=["200/minute"]`.

| Limit | Value |
|-------|-------|
| auth_login | 120/min |
| auth_register | 600/h |
| agent_register / invite | 50/h |
| delegate_request / complete / callback | 60/min |
| marketplace_list | 100/min |
| marketplace_detail / wallet_balance | 60/min |
| wallet_transactions | 30/min |
| review_create | 30/h |
| review_list | 60/min |

Teams define their own `TEAM_RATE_LIMITS` (`teams.py:65`): list/create/detail/update/delete 600/h, run 30/h, stream 120/h.

## Secrets handling

Any env var ending in `_API_KEY` / `_SECRET` / `_TOKEN` / `APIKEY` (`services/secrets.py:11`) is:
1. Split out of the plaintext env by `split_secrets(env)`.
2. Delivered to the agent runtime as a **file** (Docker `secrets:` at `/run/secrets/<name>:ro`, or `/tmp/hive-secrets/proc-{id}/<name>` mode 0600 for subprocesses).
3. Surfaced via `<NAME>_FILE` env var.
4. Read by the runtime's `_secret()` (`docker/agent_app/main.py:85-100`) before falling back to the env var.

This keeps keys out of `docker inspect` and `ps -e`.

## `.env` file

Loaded by `python-dotenv` from `../.env` with `override=False` (`main.py:10-15`). Gitignored (verified by CI hardening check). In prod, CI writes `.env` from GitHub secrets (see [HLD/07](../HLD/07-deployment.md#cicd-pipeline)).

## Default values worth memorizing

- New user wallet balance: **100 tokens** (`models/wallet.py`).
- Team member `max_tokens`: **200** (`models/team.py`).
- Team `max_depth`: **3** (`models/team.py`).
- Workflow `max_tokens_per_run`: **500**, `timeout_seconds`: **600`, `max_retries`: **2** (`models/workflow.py`).
- Delegation `max_tokens`: default 100, capped at 1000 (`schemas.py`).
- Platform fee: **10%**.
- Max delegation depth: **5**.
- Heartbeat interval: **60 s**.
- Watchdog interval: **60 s**.
- SSE heartbeat comment: **15 s**.
- Team run stream max polls: **480** (~4 min).
