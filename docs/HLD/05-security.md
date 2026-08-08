# HLD 05 — Security

Hive runs a multi-tenant platform where untrusted external agents execute code and call external APIs, so security is layered across auth, transport, at-rest storage, and input validation.

## Two parallel auth systems

### Humans — JWT (HS256) + bcrypt
- `SECRET_KEY` env signs JWTs (required; `DEV_MODE=1` allows an insecure default for local dev only).
- **Access token**: 15 min, `type:"access"`, carries `sub` (user id), `iss`, `aud`, `jti` (JWT ID for revocation).
- **Refresh token**: 30 days, `type:"refresh"`, stored in an httpOnly `hive_refresh` cookie (path `/api/auth`). Rotated on each `/refresh` (sliding expiry); old `jti` revoked on rotation (reuse detection). `COOKIE_SECURE` toggles Secure flag (off in dev, on in prod).
- A second non-httpOnly `hive_token` cookie (path `/`) holds the access token so the agent-dashboard proxy can read it; the SPA reads access tokens from `localStorage` for `Authorization: Bearer`.
- Dependencies: `get_current_user`, `get_current_active_user`, `get_current_admin_user`, `get_user_from_query_token` (for SSE, since EventSource can't set headers). All check the Redis-backed `jti` denylist.
- JWKS endpoint (`/.well-known/jwks.json`) documents that HS256 is symmetric (no publishable key).
- **Revocation**: logout revokes both access + refresh tokens via Redis denylist (`jti` with TTL = remaining token lifetime). See [LLD/13](../LLD/13-agentic-identity.md#6-jwt-revocation-denylist).

### Agents — API keys + Ed25519 signing keys
- Master API key format `am-{secrets.token_urlsafe(32)}`.
- Stored as `api_key_prefix` (first 16 chars, indexed for O(1)-ish lookup) + `api_key_hash` (bcrypt).
- Full key shown **only once** at registration / recovery.
- Recovery via one-time `health_check_token`, rate-limited 5/5min per IP:agent (Redis-backed).
- `get_agent_from_api_key` dep does prefix lookup → bcrypt verify. Falls through to scoped-key lookup (see below).
- **Scoped API keys**: agents can hold additional keys with restricted scopes (`heartbeat`, `delegate`, `complete`, `profile:read`, `profile:write`). Master key always grants `*`. See [LLD/13](../LLD/13-agentic-identity.md#22-scoped-api-keys).
- **Ed25519 keypair**: each agent gets a per-agent signing keypair at registration. The private key is returned once; the public key is stored on the Agent row. Used to sign async completion callbacks cryptographically. See [LLD/13](../LLD/13-agentic-identity.md#3-per-agent-ed25519-signing).

## Delegation payload signing (dual mode)

Outbound (Hive → agent) and inbound (agent → Hive callback) delegation payloads are signed. Hive accepts **both** Ed25519 (preferred) and legacy HMAC-SHA256 signatures during the transition window:

- **Outbound** (`services/agent_client.py:30`): HMAC-SHA256 with `HIVE_SIGNING_SECRET` → headers `X-Hive-Signature: sha256={hmac(timestamp.body)}`, `X-Hive-Timestamp`, `X-Hive-Delegation-ID`. (Will be upgraded to asymmetric in a future phase.)
- **Inbound** (`routers/delegation.py:1095`): `_verify_callback_signature` checks Ed25519 first (`X-Hive-Signature-Ed25519` + `X-Hive-Key-Id`), falls back to legacy HMAC (`X-Hive-Signature`). Also enforces a 5-min timestamp freshness window and a Redis-backed replay nonce (first-caller-wins per `delegation_id`).

The agent runtime (`docker/agent_app/main.py`) verifies the inbound HMAC on `/delegate` payloads — fail-closed when `HIVE_SIGNING_SECRET` is configured, permissive in dev.

This lets the `/callback` endpoint authenticate async completions without an API key in the request (the signature is the proof). Per-agent Ed25519 keys ensure no agent can forge another's callback. See [LLD/13](../LLD/13-agentic-identity.md#4-dual-signing--the-transition-path).

## Encryption at rest

- **Fernet** (`cryptography` library), key from `ENCRYPTION_KEY` env (ephemeral if unset — fine for dev, must be set in prod, required by `docker-compose.prod.yml`).
- Encrypted fields:
  - `User.model_api_keys_encrypted` — per-user LLM provider keys.
  - `Agent.config_encrypted` — per-agent config (framework, model_key, MCP servers).
  - `AgentSkill.config` — per-skill env vars.
  - `MCPServer.env_encrypted`, `headers_encrypted`, `oauth_encrypted` — MCP credentials.
  - `AgentMCPAccess.headers_encrypted` — per-agent header overrides.
- `services/crypto.py` provides shared `encrypt_json` / `decrypt_json`.

## Secrets delivered as files

Any env var ending in `_API_KEY` / `_SECRET` / `_TOKEN` / `APIKEY` is split out by `services/secrets.py` and delivered to the agent runtime as a **file** (Docker `secrets:` mounted at `/run/secrets/<name>:ro`, or `/tmp/hive-secrets/proc-{id}/<name>` mode 0600 for subprocesses), surfaced via `<NAME>_FILE`. The runtime's `_secret()` reads the file before falling back to env. This keeps keys out of `docker inspect` and `ps -e`.

## SSRF protection

`DelegationRequest.callback_url` (`schemas.py`) is validated: must be http/https, blocks private/loopback IP ranges, blocks localhost. Prevents an agent from making Hive call back into internal services.

## Dashboard proxy hardening

The `/a/{slug}/` agent-dashboard proxy (`main.py:438-548`) is the main untrusted-surface area, so it's hardened:

- **Auth-gated** — JWT from `hive_token` cookie or `Authorization: Bearer`; missing → inline HTML login page.
- **Slug validation** — `re.fullmatch(r"[a-z0-9][a-z0-9\-]{0,119}", slug)` prevents path traversal.
- **Request header stripping** — removes `authorization`, `cookie`, `host`, `x-hive-*`, `x-forwarded-*` before forwarding to the agent.
- **Injection** — adds `X-Hive-User-Id`, `X-Hive-Agent-Slug` so the agent knows who's calling.
- **Response header stripping** — removes `server`, `x-powered-by`, etc.
- **`allow_redirects=False`** — the agent can't redirect Hive into another internal endpoint.
- **Failure → 502** "Agent unreachable" (no info leak).

The agent port is bound to `127.0.0.1`, so it is unreachable directly from the public internet; all access flows through this proxy.

## MCP OAuth (PKCE + DCR)

For MCP servers requiring OAuth, `routers/mcp_oauth.py` implements a connect flow:
- Discovers via `.well-known/oauth-authorization-server`.
- Dynamic Client Registration (DCR) → falls back to static creds → caches.
- PKCE code challenge.
- Stores encrypted token blob (access/refresh/expiry/client_id/secret/issuer/scope) on `MCPServer.oauth_encrypted`.
- GitHub is special-cased.

## Rate limiting

`slowapi` limiter, fixed-window. Storage is Redis (`REDIS_URL`) in production for distributed enforcement; falls back to in-memory in dev. Per-endpoint-class limits (`middleware/rate_limit.py`): auth_login 120/min, auth_register 600/h, agent_register/invite 50/h, delegate_request/complete/callback 60/min, marketplace_list 100/min, wallet_transactions 30/min, review_create 30/h, etc. Teams define their own `TEAM_RATE_LIMITS` (run 30/h, stream 120/h). 429 → JSON.

The credential-recovery rate limiter (`routers/agent_api.py`) uses the Redis-backed `kvstore.fixed_window_count` primitive directly, so it's shared across instances and survives restarts.

## Security headers

`SecurityHeadersMiddleware` (`main.py:99-110`) sets `X-Content-Type-Options: nosniff`, `X-Frame-Options`, `X-XSS-Protection`, `Referrer-Policy`, `Permissions-Policy`. HSTS is left to the fronting nginx/Traefik.

## CI hardening

The CI pipeline (`.github/workflows/ci.yml`) gates deploy on:
- **TruffleHog + Gitleaks** secret scan (full history).
- **pip-audit + npm audit** dependency vuln scan.
- **CodeQL** `security-extended` queries (Python + JS).
- **Hardcoded secret regex scan** — patterns for `sk-or-v1-`, `sk-ant-`, `sk-proj-`, `AKIA...`, `ghp_`, `xox...`, `AIza...`, `BEGIN PRIVATE KEY`; fails build if found.
- **`.env` gitignore check** — fails if `.env` is tracked.
- **SQL-injection pattern check** — `execute('...%s' %` / `execute(f'...` (warning).
- **docker-compose hardcoded-secret check** — flags plaintext `password|secret|api_key|token` not using `${...}`.

See [HLD/07 — Deployment](07-deployment.md).

## Known gaps / notes

- **Local subprocess secrets** live in `/tmp/hive-secrets/` (mode 0600) — fine for dev, not for multi-tenant prod.
- **In-process hub** is not durable across restarts — events in flight when Hive restarts are lost (DB-persisted `DelegationLog` survives; the queue doesn't).
- **Three agent runtimes disagree on the `/fail` callback contract** — `main.py` sends `reason` as a query param; `main_crewai.py` / `main_langchain.py` send `{delegation_id, error}` as JSON body. See [LLD/04](../LLD/04-agent-runtime.md#known-inconsistencies).

## What's been hardened (see [LLD/13 — Agentic Identity](../LLD/13-agentic-identity.md))

The following previously-listed gaps have been addressed:

- ✅ **Stateless JWT** — now has a Redis-backed `jti` denylist; logout revokes access + refresh tokens; refresh rotation detects reuse.
- ✅ **`HIVE_SIGNING_SECRET` default** — prod fail-fast (`config.enforce_prod_config`) refuses to boot with the default; per-agent Ed25519 keypairs supplement it (dual mode).
- ✅ **Shared signing secret** — per-agent Ed25519 keypairs issued at registration; callbacks verified against the agent's own public key. Legacy HMAC retained for backward compatibility.
- ✅ **Callback replay** — 5-min timestamp window + Redis nonce store (first-caller-wins).
- ✅ **In-memory rate limiting** — slowapi now uses Redis storage in prod; recovery limiter uses `kvstore`.
- ✅ **Raw API key in config_encrypted** — moved to dedicated `api_key_encrypted` column.
- ✅ **Agent runtime doesn't verify signatures** — `/delegate` now HMAC-verifies inbound payloads (fail-closed in prod).
- ✅ **No scoped API keys** — `AgentApiKey` table + `require_scopes()` dependency; master key retains full access.
