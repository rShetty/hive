# HLD 05 — Security

Hive runs a multi-tenant platform where untrusted external agents execute code and call external APIs, so security is layered across auth, transport, at-rest storage, and input validation.

## Two parallel auth systems

### Humans — JWT (HS256) + bcrypt
- `SECRET_KEY` env signs JWTs (required; `DEV_MODE=1` allows an insecure default for local dev only).
- **Access token**: 15 min, `type:"access"`, carries `sub` (user id), `iss`, `aud`.
- **Refresh token**: 30 days, `type:"refresh"`, stored in an httpOnly `hive_refresh` cookie (path `/api/auth`). Rotated on each `/refresh` (sliding expiry). `COOKIE_SECURE` toggles Secure flag (off in dev, on in prod).
- A second non-httpOnly `hive_token` cookie (path `/`) holds the access token so the agent-dashboard proxy can read it; the SPA reads access tokens from `localStorage` for `Authorization: Bearer`.
- Dependencies: `get_current_user`, `get_current_active_user`, `get_current_admin_user`, `get_user_from_query_token` (for SSE, since `EventSource` can't set headers).
- JWKS endpoint (`/.well-known/jwks.json`) documents that HS256 is symmetric (no publishable key).
- **No server-side revocation** — stateless JWT. Logout only clears the refresh cookie. Short access-token lifetime is the mitigation.

### Agents — API keys
- Format `am-{secrets.token_urlsafe(32)}`.
- Stored as `api_key_prefix` (first 16 chars, indexed for O(1)-ish lookup) + `api_key_hash` (bcrypt).
- Full key shown **only once** at registration / recovery.
- Recovery via one-time `health_check_token`, rate-limited 5/5min per IP:agent.
- `get_agent_from_api_key` dep does prefix lookup → bcrypt verify.

## HMAC-signed delegation payloads

Outbound (Hive → agent) and inbound (agent → Hive callback) delegation payloads are HMAC-SHA256 signed with `HIVE_SIGNING_SECRET`:

- Outbound (`services/agent_client.py:30`): headers `X-Hive-Signature: sha256={hmac(timestamp.body)}`, `X-Hive-Timestamp`, `X-Hive-Delegation-ID`.
- Inbound (`routers/delegation.py:1061`): `_verify_callback_signature` checks `X-Hive-Signature` + `X-Hive-Timestamp`, timing-safe `hmac.compare_digest`.

This lets the `/callback` endpoint authenticate async completions without an API key in the request (the signature is the proof).

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

`slowapi` limiter, fixed-window, in-memory storage. Per-endpoint-class limits (`middleware/rate_limit.py`): auth_login 120/min, auth_register 600/h, agent_register/invite 50/h, delegate_request/complete/callback 60/min, marketplace_list 100/min, wallet_transactions 30/min, review_create 30/h, etc. Teams define their own `TEAM_RATE_LIMITS` (run 30/h, stream 120/h). 429 → JSON.

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

- **Stateless JWT** — no revocation list; a stolen access token is valid until expiry (15 min). Refresh-token rotation mitigates replay but there's no detection of reuse.
- **`HIVE_SIGNING_SECRET` default `change-me-in-production`** — must be overridden in prod.
- **Local subprocess secrets** live in `/tmp/hive-secrets/` (mode 0600) — fine for dev, not for multi-tenant prod.
- **In-process hub** is not durable across restarts — events in flight when Hive restarts are lost (DB-persisted `DelegationLog` survives; the queue doesn't).
- **Three agent runtimes disagree on the `/fail` callback contract** — `main.py` sends `reason` as a query param; `main_crewai.py` / `main_langchain.py` send `{delegation_id, error}` as JSON body. See [LLD/04](../LLD/04-agent-runtime.md#known-inconsistencies).
