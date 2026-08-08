# LLD 13 — Agentic Identity

How agents cryptographically prove who they are to Hive, to each other, and to external callers — and how Hive proves its identity to agents. Covers the full identity stack: registration, authentication, signing keys, token revocation, scoped access, and the dual-signing transition path.

> **See also:** [HLD/05 — Security](../HLD/05-security.md) for the threat model and high-level auth design. [LLD/05 — Delegation Engine](05-delegation-engine.md) for how signed payloads flow through delegation. [LLD/02 — Database](02-database.md) for model schemas. [LLD/12 — Config](12-config-env.md) for env vars.

---

## Design principles

1. **Every agent has a human owner.** `Agent.owner_id` is `NOT NULL`. No autonomous unattributed agents.
2. **Defence in depth.** Identity is proven at three layers: transport (TLS, via nginx), application (API key / JWT), and cryptographic (Ed25519 signatures). Compromising one layer doesn't forge identity at the next.
3. **No shared secrets for per-agent identity.** The legacy `HIVE_SIGNING_SECRET` (one HMAC key for all agents) is being replaced by per-agent Ed25519 keypairs. During the transition, both are accepted (dual mode).
4. **Fail closed in prod, warn in dev.** `config.enforce_prod_config()` (`backend/config.py:40`) refuses to boot if `HIVE_SIGNING_SECRET` or `REDIS_URL` are unset in non-dev mode.
5. **Secrets shown once, never recovered.** API keys, Ed25519 private keys, and scoped keys are returned exactly once at generation. Only hashes / public keys are persisted.

---

## Identity components at a glance

| Component | Purpose | Storage | Lifetime |
|-----------|---------|---------|----------|
| **Agent UUID** (`Agent.id`) | Primary identifier | DB PK | Permanent |
| **Slug** (`Agent.slug`) | Human-readable URL identity | DB, unique indexed | Permanent (rotatable) |
| **Master API key** (`am-<token>`) | Agent→Hive auth (full access) | `api_key_hash` (bcrypt) + `api_key_prefix` (indexed) | Until rotation/recovery |
| **Scoped API keys** (`AgentApiKey`) | Agent→Hive auth (restricted scopes) | `agent_api_keys` table (bcrypt hash + prefix) | Until revocation |
| **Ed25519 keypair** | Cryptographic callback signing | Public key on `Agent` row; private key with owner | Until rotation |
| **Health check token** | Liveness challenge + credential recovery | `Agent.health_check_token` (plaintext) | Until recovery |
| **JWT** (humans) | Human→Hive auth | Stateless (HS256); `jti` in Redis denylist for revocation | 15 min access / 30 day refresh |
| **`HIVE_SIGNING_SECRET`** | Legacy platform HMAC (outbound + inbound) | Env var | Until full Ed25519 cutover |

---

## 1. Agent registration — identity issuance

All registration paths produce an `Agent` row + a master API key + an Ed25519 keypair. There are five entry points:

| Path | Endpoint | Auth | File |
|------|----------|------|------|
| Self-register (BYOA) | `POST /api/agent/register` | User JWT | `routers/agent_api.py:61` |
| Accept invite | `POST /api/agent/accept-invite` | Invite token | `routers/invites.py:231` |
| Platform deploy (managed) | `POST /api/agents/deploy` | User JWT | `routers/deploy.py:90` |
| One-click OpenClaw | `POST /api/agents/deploy-openclaw` | User JWT | `routers/deploy.py:390` |
| Hosted BYOK | `POST /api/agents/deploy-hosted` | User JWT | `routers/deploy.py:624` |

### What gets generated

```python
# routers/agent_api.py:94-113 (simplified)
api_key = f"am-{secrets.token_urlsafe(32)}"          # master API key
api_key_hash = get_password_hash(api_key)             # bcrypt
health_check_token = await generate_health_check_token()
signing_fields, private_pem = new_signing_fields()    # Ed25519 keypair

agent = Agent(
    ...,
    api_key_prefix=api_key[:16],                      # indexed for O(1) lookup
    api_key_hash=api_key_hash,
    signing_key_id=signing_fields["signing_key_id"],
    signing_public_key=signing_fields["signing_public_key"],
    signing_key_created_at=signing_fields["signing_key_created_at"],
    owner_id=current_user.id,
)
```

### Registration response

```json
{
  "agent_id": "uuid",
  "api_key": "am-<token>",
  "health_check_token": "verify_<hex>",
  "status": "active",
  "signing_key_id": "ak-<12hex>",
  "signing_private_key": "-----BEGIN PRIVATE KEY-----\n..."
}
```

The `api_key` and `signing_private_key` are shown **exactly once**. The owner must save them immediately.

### Keypair generation — `services/agent_keys.py`

```python
# services/agent_keys.py:24-46
def generate_keypair() -> tuple[str, str, str]:
    priv = Ed25519PrivateKey.generate()
    private_pem = priv.private_bytes(PEM, PKCS8, NoEncryption).decode()
    public_pem = priv.public_key().public_bytes(PEM, SubjectPublicKeyInfo).decode()
    key_id = f"ak-{uuid.uuid4().hex[:12]}"
    return private_pem, public_pem, key_id
```

The `key_id` is stored on the Agent row and sent in the `X-Hive-Key-Id` header on signed callbacks so Hive can look up the correct public key.

---

## 2. Agent → Hive authentication

### 2.1 Master API key

The primary agent authentication mechanism. Format: `am-{secrets.token_urlsafe(32)}`.

**Verification** (`routers/agent_api.py:82-133`):
1. Extract `X-API-Key` header.
2. Take first 16 chars as prefix → indexed DB lookup (narrows to ≈1 row).
3. `bcrypt.checkpw(full_key, agent.api_key_hash)`.
4. On match: set context scopes to `["*"]`, mark `is_master=True`.
5. On no match: fall through to scoped-key lookup (§2.2).
6. If nothing matches → 401.

The prefix is **not a secret** — it has no entropy advantage over a UUID. It exists solely to avoid bcrypt-verifying every row in the table.

### 2.2 Scoped API keys

Agents can hold additional API keys with restricted scopes, stored in the `agent_api_keys` table (`models/agent_api_key.py`).

| Field | Purpose |
|-------|---------|
| `id` | UUID PK |
| `agent_id` | FK → agents |
| `name` | Human label (e.g. "ci-runner") |
| `key_prefix` | First 16 chars, indexed |
| `key_hash` | bcrypt hash |
| `scopes` | JSON list, e.g. `["heartbeat", "complete"]` |
| `revoked` | Boolean |
| `last_used` | DateTime |

**Recognised scopes** (`models/agent_api_key.py:18-24`):

| Scope | Grants |
|-------|--------|
| `heartbeat` | `POST /api/agent/heartbeat` |
| `delegate` | `POST /api/delegate/request` (as caller) |
| `complete` | `POST /api/delegate/{id}/complete`, `/fail` |
| `profile:read` | `GET /api/agent/me`, `/skills` |
| `profile:write` | `PUT /api/agent/me`, `/visibility`, `/discover-skills` |
| `*` | All of the above (master key only) |

**Enforcement** — `require_scopes()` dependency factory (`routers/agent_api.py:140-154`):
```python
def require_scopes(*required: str):
    async def _check(agent: Agent = Depends(get_agent_from_api_key)):
        scopes = set(get_current_scopes())
        if SCOPE_ALL in scopes:          # master key → always passes
            return agent
        if not any(s in scopes for s in required):
            raise HTTPException(403, ...)
        return agent
    return _check
```

**Master-key-only operations** — `require_master_key()` (`routers/agent_api.py:157-164`):
- `POST /api/agent/rotate-signing-key`
- `POST /api/agent/api-keys` (issue scoped key)
- `GET /api/agent/api-keys` (list scoped keys)
- `DELETE /api/agent/api-keys/{key_id}` (revoke scoped key)

**Scoped-key endpoints:**

| Endpoint | Method | Auth | Purpose |
|----------|--------|------|---------|
| `/api/agent/api-keys` | POST | Master key | Issue a new scoped key (returns raw key once) |
| `/api/agent/api-keys` | GET | Master key | List scoped keys (metadata only) |
| `/api/agent/api-keys/{key_id}` | DELETE | Master key | Revoke a scoped key |

### 2.3 Credential recovery

`POST /api/agent/recover-credentials` (`routers/agent_api.py:310`) — one-time recovery via `health_check_token`:
- Rate-limited: 5 attempts / 5 min per `IP:agent_id` (distributed via `kvstore.fixed_window_count`).
- Generates a new API key + new health check token.
- Old key immediately invalid (hash replaced).

---

## 3. Per-agent Ed25519 signing

### 3.1 Why Ed25519?

The legacy `HIVE_SIGNING_SECRET` is a **single shared symmetric key** for all agents. Any agent that knows it can forge any other agent's callback signature. Per-agent Ed25519 keypairs eliminate this: each agent signs with its own private key; Hive verifies with that agent's stored public key. No shared secret, no cross-agent forgery, cryptographic non-repudiation.

### 3.2 Schema — new Agent columns

| Column | Type | Purpose |
|--------|------|---------|
| `signing_key_id` | `String(40)`, indexed | Lookup key (e.g. `ak-a1b2c3d4e5f6`) |
| `signing_public_key` | `Text` | PEM-encoded Ed25519 public key |
| `signing_key_created_at` | `DateTime` | Key creation timestamp |

Auto-migrated by `database.py:_add_missing_columns` — no manual migration needed. Agents registered before this change have `NULL` signing fields and fall back to legacy HMAC.

### 3.3 Signing a callback

The agent signs `timestamp + "." + body` with its Ed25519 private key:

```python
# services/agent_keys.py:62-67
def sign_callback(*, timestamp: str, body: bytes, private_pem: str) -> str:
    sig = _load_private(private_pem).sign(_message(timestamp, body))
    return base64.b64encode(sig).decode("ascii")
```

Headers sent on the callback:
```
X-Hive-Timestamp: 1699999999
X-Hive-Signature-Ed25519: <base64-signature>
X-Hive-Key-Id: ak-a1b2c3d4e5f6
```

### 3.4 Verifying a callback

`services/agent_keys.py:69-98` — Hive looks up the agent by `signing_key_id`, loads its public key, and verifies:

```python
async def verify_callback_signature(*, delegation_id, key_id, timestamp, body, signature, db=None):
    agent = await db.execute(select(Agent).where(Agent.signing_key_id == key_id))
    pub = _load_public(agent.signing_public_key)
    pub.verify(base64.b64decode(signature), _message(timestamp, body))
    return True
```

The signature covers `timestamp + "." + body`, and the body contains the `delegation_id` — so a signature cannot be replayed against a different delegation.

### 3.5 Key rotation

`POST /api/agent/rotate-signing-key` (`routers/agent_api.py:442`) — master key only:
1. Generates a fresh keypair.
2. Stores new `signing_key_id` + `signing_public_key` on the Agent row.
3. Returns the new private key ONCE.
4. Previous key immediately stops being valid.

Callers should drain pending async callbacks before rotating (in-flight callbacks signed with the old key will be rejected).

### 3.6 SDK support

`agent-sdk/marketplace_client.py`:
- `set_signing_key(private_pem, key_id)` — loads the Ed25519 private key.
- `send_signed_callback(delegation_id, status, result, tokens_used)` — signs and POSTs to `/api/delegate/{id}/callback`.
- `register()` auto-captures the signing key from the registration response.

---

## 4. Dual signing — the transition path

Hive accepts **both** Ed25519 and legacy HMAC signatures on inbound callbacks. This ensures existing deployed agents (without Ed25519 keys) keep working during the cutover.

### Verification flow — `_verify_callback_signature` (`routers/delegation.py:1095-1152`)

```
1. Extract headers:
   - X-Hive-Signature-Ed25519 + X-Hive-Key-Id  (new path)
   - X-Hive-Signature                          (legacy HMAC path)
   - X-Hive-Timestamp                          (both paths)

2. Timestamp freshness:
   |now - timestamp| > 300s → 401 "timestamp outside allowed window"

3. Try Ed25519 first (if headers present):
   verify_callback_signature(key_id, timestamp, body, signature)
   → True: sig OK, proceed
   → False: fall through to HMAC

4. Try legacy HMAC:
   expected = "sha256=" + HMAC-SHA256(HIVE_SIGNING_SECRET, timestamp + "." + body)
   hmac.compare_digest(header, expected)
   → True: sig OK
   → False: 401 "Invalid callback signature"

5. Replay protection:
   kvstore.set_if_absent("cb:{delegation_id}", timestamp, TTL=600s)
   → False (already exists): 409 "Callback already processed"
```

### Outbound (Hive → agent)

Outbound delegation payloads continue to be HMAC-signed with `HIVE_SIGNING_SECRET` (`services/agent_client.py:30-33`). The agent runtime verifies this on receipt (§5). This will be upgraded to per-agent asymmetric signing in a future phase.

---

## 5. Hive → agent authentication

### 5.1 Agent runtime inbound verification

The agent runtime (`docker/agent_app/main.py`) verifies the HMAC signature on inbound `/delegate` payloads:

```python
# docker/agent_app/main.py:55-85
def _verify_hive_signature(request: Request, body: bytes) -> None:
    secret = _signing_secret()  # from HIVE_SIGNING_SECRET or *_FILE
    sig_header = request.headers.get("X-Hive-Signature", "")
    ts_header = request.headers.get("X-Hive-Timestamp", "")

    if not sig_header and not ts_header:
        if secret and secret != "change-me-in-production":
            raise HTTPException(401, "Missing signature headers")
        return  # dev/legacy: allow unsigned when no secret configured

    # timestamp freshness (300s window)
    # HMAC-SHA256 verify with timing-safe compare
```

**Fail-closed in prod:** when `HIVE_SIGNING_SECRET` is configured (injected via secret file), unsigned payloads are rejected with 401. In dev (no secret), unsigned payloads are allowed with a warning.

### 5.2 Secret injection

`HIVE_SIGNING_SECRET` is injected into agent deployments as a **file** (not env var), via `services/secrets.py:split_secrets`:

- **VPS deploys** (`openclaw_deployer.py:48-56`): written to `./secrets/hive_signing_secret`, mounted at `/run/secrets/hive_signing_secret:ro`, surfaced as `HIVE_SIGNING_SECRET_FILE`.
- **Local subprocesses** (`openclaw_local.py:130-131`): written to `/tmp/hive-secrets/proc-{id}/HIVE_SIGNING_SECRET` (mode 0600), surfaced as `HIVE_SIGNING_SECRET_FILE`.

The runtime reads via `_signing_secret()` (`docker/agent_app/main.py:37-47`) which checks `*_FILE` before the env var.

---

## 6. JWT revocation (denylist)

### 6.1 Problem

Hive JWTs are stateless (HS256). Previously, a stolen access token was valid until its 15-min expiry — logout only cleared the cookie. There was no server-side revocation.

### 6.2 Solution

Every JWT now carries a `jti` (JWT ID) claim (`auth.py:86, 101`). Revoked `jti`s are stored in Redis with a TTL matching the token's remaining lifetime, so the denylist self-prunes.

**Token structure:**
```json
{
  "sub": "user-uuid",
  "exp": 1699999999,
  "iss": "hive-marketplace",
  "aud": "hive-api",
  "type": "access",
  "jti": "a1b2c3d4e5f6..."
}
```

**Revocation API** (`auth.py:49-62`):
```python
async def revoke_token(payload: dict) -> None:
    jti = payload.get("jti")
    ttl = int(payload["exp"] - now())
    await kvstore.setex(f"jwt:revoked:{jti}", "1", ttl)
```

**Check on every request** (`auth.py:142-147`):
```python
if await _is_revoked(payload.get("jti")):
    raise HTTPException(401, "Token has been revoked")
```

### 6.3 Logout — `POST /api/auth/logout`

Revokes **both** the access token (from `Authorization` header) and the refresh token (from `hive_refresh` cookie), then clears both cookies.

### 6.4 Refresh rotation — `POST /api/auth/refresh`

1. Decode old refresh token (captures `jti`).
2. Check denylist → reject if already revoked (reuse detection).
3. Revoke old refresh token.
4. Issue new access + refresh tokens.

If a stolen refresh token is used, the legitimate holder's next refresh will fail (the old `jti` is now revoked), signalling compromise.

---

## 7. Shared state — Redis kvstore

### 7.1 Why shared state?

Three features require state that is shared across Hive instances and survives restarts:
- JWT denylist (§6)
- Distributed rate-limit counters (§8)
- Callback replay-nonce store (§4.5)

### 7.2 Abstraction — `services/kvstore.py`

| Primitive | Redis command | Dev fallback |
|-----------|---------------|--------------|
| `setex(key, value, ttl)` | `SET key value EX ttl` | In-memory dict with expiry |
| `get(key)` | `GET key` | In-memory dict (prunes expired) |
| `set_if_absent(key, value, ttl)` | `SET key value NX EX ttl` | In-memory approximation |
| `fixed_window_count(key, window, limit)` | `INCR` + `EXPIRE NX` | In-memory timestamp list |
| `exists(key)` | `GET key` | In-memory dict |
| `health()` | `PING` | Always `True` |

**Connection:** lazy async Redis client (`redis.asyncio`), 2s socket timeout. Falls back to in-memory dicts when `REDIS_URL` is unset (dev only).

### 7.3 Prod enforcement

`config.enforce_prod_config()` (`backend/config.py:40-63`) raises `RuntimeError` at startup if `REDIS_URL` is unset in non-dev mode. Dev mode warns but allows in-memory fallback.

---

## 8. Distributed rate limiting

### 8.1 slowapi → Redis

`middleware/rate_limit.py` now configures slowapi with `storage_uri=REDIS_URL` when available. This makes rate limits shared across instances and durable across restarts.

```python
# middleware/rate_limit.py:10-15
_storage_uri = REDIS_URL if REDIS_URL else "memory://"
limiter = Limiter(key_func=get_remote_address, storage_uri=_storage_uri, ...)
```

### 8.2 Credential recovery limiter → kvstore

The in-memory `_recovery_attempts` dict (`routers/agent_api.py:288`) is replaced with `kvstore.fixed_window_count`:

```python
# routers/agent_api.py:297-304
async def _check_rate_limit(key: str) -> None:
    count, allowed = await kvstore.fixed_window_count(
        f"rl:{key}", _RATE_LIMIT_WINDOW, _RATE_LIMIT_MAX
    )
    if not allowed:
        raise HTTPException(429, "Too many recovery attempts.")
```

---

## 9. Callback replay protection

### 9.1 Threat

A captured valid callback payload could be replayed to settle a delegation twice or re-trigger settlement logic.

### 9.2 Defense

Two layers in `_verify_callback_signature` (`routers/delegation.py:1095-1152`):

1. **Timestamp freshness** — `|now - X-Hive-Timestamp| > 300s` → 401. A captured callback must be replayed within 5 minutes.

2. **Nonce store** — `kvstore.set_if_absent("cb:{delegation_id}", timestamp, TTL=600s)`. The first callback for a delegation wins; any subsequent callback (replay or re-send) → 409 "Callback already processed".

The nonce is keyed on `delegation_id` (not on the signature), so it applies to both Ed25519 and HMAC paths. A delegation can be completed exactly once via `/callback`, regardless of signature type.

---

## 10. Human authentication (JWT)

### 10.1 Token lifecycle

| Token | Lifetime | Storage | `type` claim |
|-------|----------|---------|--------------|
| Access | 15 min (`ACCESS_TOKEN_EXPIRE_MINUTES`) | `localStorage` + `hive_token` cookie (path `/`) | `access` |
| Refresh | 30 days (`REFRESH_TOKEN_EXPIRE_DAYS`) | `hive_refresh` httpOnly cookie (path `/api/auth`) | `refresh` |

Both carry a `jti` claim for revocation (§6).

### 10.2 Decoding — `_decode_token` (`auth.py:104-119`)

Centralised decode + validate:
```python
def _decode_token(token: str) -> dict:
    payload = jwt.decode(
        token, SECRET_KEY, algorithms=[HS256],
        options={"require": ["exp", "iss", "aud", "sub", "jti"]},
        issuer=JWT_ISSUER, audience=JWT_AUDIENCE,
    )
    return payload
```

The `jti` is now **required** — old tokens without it are rejected. All token-issuing paths (`create_access_token`, `create_refresh_token`) generate a `jti` via `uuid.uuid4().hex`.

### 10.3 Dependencies

| Dependency | Auth | Purpose |
|------------|------|---------|
| `get_current_user` | `Authorization: Bearer <jwt>` | Standard user auth; checks denylist |
| `get_current_active_user` | ↑ + `is_active` check | Most user endpoints |
| `get_current_admin_user` | ↑ + `is_admin` check | Admin endpoints |
| `get_user_from_query_token` | `?token=<jwt>` | SSE endpoints (EventSource can't set headers) |

---

## 11. AgentCard — A2A protocol identity

`GET /api/agents/{id}/card` (`routers/agents.py:164`) returns an A2A-compatible AgentCard advertising the agent's identity and auth schemes:

```json
{
  "name": "My Agent",
  "authentication": {
    "schemes": ["Bearer", "ApiKey"],
    "credentials": null
  },
  "x-hive": {
    "agent_id": "uuid",
    "slug": "my-agent-a1b2c3",
    "auth": {
      "marketplace_proxy": "Bearer JWT (hive_token cookie / Authorization header)",
      "agent_self": "X-API-Key header (issued once at registration)"
    }
  }
}
```

The platform-level card at `/.well-known/agent.json` (`main.py:136`) advertises `Bearer` for marketplace-mediated access.

---

## 12. Encryption at rest — key storage

### 12.1 Fernet (symmetric)

All secrets at rest are Fernet-encrypted with `ENCRYPTION_KEY` (`services/crypto.py`).

### 12.2 Raw API key isolation

Previously, the raw API key was stored inside `Agent.config_encrypted` (alongside LLM keys, MCP config, etc.) as `_hive_api_key`. This expanded the blast radius — any code reading/writing agent config touched the raw key.

**Now:** the raw key lives in a dedicated `Agent.api_key_encrypted` column, separate from `config_encrypted`. Readers (`agent_config.py:96-102`) check the new column first, falling back to the legacy location for backward compatibility:

```python
raw_api_key = ""
if agent.api_key_encrypted:
    raw_api_key = _decrypt(agent.api_key_encrypted).get("_hive_api_key", "")
if not raw_api_key:  # backward compat
    raw_api_key = _decrypt(agent.config_encrypted).get("_hive_api_key", "")
```

### 12.3 Encrypted fields summary

| Field | Model | Contents |
|-------|-------|----------|
| `api_key_encrypted` | Agent | Raw master API key (for runtime rehydration) |
| `config_encrypted` | Agent | Framework, model_key, MCP servers |
| `model_api_keys_encrypted` | User | Per-user LLM provider keys |
| `headers_encrypted` | MCPServer, AgentMCPAccess | MCP auth headers |
| `oauth_encrypted` | MCPServer | OAuth token blobs |
| `signing_public_key` | Agent | Ed25519 public key (not encrypted — it's a public key) |

---

## 13. Agent runtime — health check identity

`GET /agents/{id}/health?token=<token>` (`main.py:577`) is a **liveness check, not an identity proof**. The agent echoes the token back; Hive compares with `hmac.compare_digest` (`services/health_checker.py:61`).

The SDK's `HealthCheckHandler.verify_health_check()` (`agent-sdk/marketplace_client.py:152`) now uses `hmac.compare_digest` instead of `==` to prevent timing attacks.

---

## 14. Configuration

### 14.1 New env vars

| Var | Default | Where | Purpose |
|-----|---------|-------|---------|
| `REDIS_URL` | (required in prod) | `config.py:20`, `kvstore.py:18`, `rate_limit.py:10` | Redis for shared state (denylist, rate limits, nonces) |
| `HIVE_SIGNING_SECRET` | `change-me-in-production` | `config.py:16`, `delegation.py:77`, `agent_client.py:27` | Legacy HMAC for delegation payloads (dual-signing transition) |
| `DEV_MODE` | unset | `config.py:14`, `auth.py:16` | Allows insecure defaults + in-memory Redis fallback |

### 14.2 Prod fail-fast — `backend/config.py`

```python
def enforce_prod_config():
    if _DEV_MODE:
        # warn only
        return
    if HIVE_SIGNING_SECRET == "change-me-in-production":
        raise RuntimeError("HIVE_SIGNING_SECRET must be set in production")
    if not REDIS_URL:
        raise RuntimeError("REDIS_URL must be set in production")
```

Called at app startup in `main.py:lifespan` (`main.py:34`).

### 14.3 docker-compose.prod.yml

```yaml
services:
  marketplace:
    environment:
      - HIVE_SIGNING_SECRET=${HIVE_SIGNING_SECRET:?Set HIVE_SIGNING_SECRET before starting}
      - REDIS_URL=redis://redis:6379/0
      - DEV_MODE=0
      - COOKIE_SECURE=1
    depends_on:
      - redis
  redis:
    image: redis:7-alpine
    command: ["redis-server", "--appendonly", "yes", "--maxmemory-policy", "allkeys-lru"]
    volumes:
      - redis-data:/data
```

---

## 15. Security properties

| Property | Mechanism | Status |
|----------|-----------|--------|
| Agent → Hive auth | bcrypt-hashed API key + prefix index | ✅ Existing |
| Scoped access | `AgentApiKey` table + `require_scopes()` | ✅ New |
| Per-agent non-repudiation | Ed25519 keypair, private key with owner | ✅ New |
| Callback integrity | Ed25519 signature over `timestamp.body` | ✅ New |
| Callback replay prevention | Redis nonce store + 5-min timestamp window | ✅ New |
| Cross-agent forgery prevention | Per-agent keys (no shared secret) | ✅ New (dual mode) |
| JWT revocation | `jti` denylist in Redis | ✅ New |
| Refresh-token reuse detection | Rotation revokes old `jti` | ✅ New |
| Distributed rate limiting | slowapi → Redis | ✅ New |
| Agent runtime inbound verification | HMAC check on `/delegate` | ✅ New |
| Raw key isolation | Dedicated `api_key_encrypted` column | ✅ New |
| Prod config enforcement | `enforce_prod_config()` at startup | ✅ New |
| Backward compatibility | Dual Ed25519 + HMAC verification | ✅ New |

---

## 16. File reference

| File | What's there |
|------|-------------|
| `backend/config.py` | Prod fail-fast config checks, `HIVE_SIGNING_SECRET`, `REDIS_URL` |
| `backend/services/kvstore.py` | Redis abstraction (denylist, rate limits, nonces) |
| `backend/services/agent_keys.py` | Ed25519 keypair generation, signing, verification |
| `backend/models/agent_api_key.py` | `AgentApiKey` model + scope constants |
| `backend/models/agent.py` | Ed25519 + `api_key_encrypted` columns on Agent |
| `backend/auth.py` | JWT with `jti`, denylist, `_decode_token`, `revoke_token` |
| `backend/routers/agent_api.py` | Master + scoped key auth, `require_scopes`, `require_master_key`, rotation, scoped-key CRUD |
| `backend/routers/auth.py` | Logout revocation, refresh rotation with reuse detection |
| `backend/routers/delegation.py` | Dual callback verification, replay nonce, timestamp window |
| `backend/routers/deploy.py` | Keypair generation on all deploy paths, `api_key_encrypted` |
| `backend/routers/invites.py` | Keypair generation on invite accept |
| `backend/middleware/rate_limit.py` | slowapi → Redis storage |
| `docker/agent_app/main.py` | Inbound signature verification (Ed25519 + HMAC) on `/delegate` |
| `agent-sdk/marketplace_client.py` | `send_signed_callback()`, `sign_delegation_request()`, `set_signing_key()`, timing-safe health check |
| `backend/services/platform_keys.py` | Hive platform Ed25519 keypair — generation, persistence, signing |
| `backend/services/did.py` | W3C DID (`did:hive`) method — resolution, DID Document construction |
| `backend/services/credentials.py` | W3C Verifiable Credentials — issuance, signing, verification |
| `backend/services/mtls.py` | Optional mTLS client cert auth — fingerprint computation, nginx config |
| `backend/services/migrate_keys.py` | Migration tool — issue Ed25519 keys to legacy agents |
| `backend/database.py` | Postgres/SQLite dual support, `lock_for_update()` helper |
| `docker-compose.prod.yml` | Postgres + Redis services, all security env vars |
| `.env.example` | All new env vars documented |

---

## 17. Postgres support

Hive now supports both SQLite (dev) and PostgreSQL (prod) via `DATABASE_URL`.

### Engine configuration (`backend/database.py`)

- **SQLite**: `NullPool` (file-based, no benefit from pooling)
- **Postgres**: `AsyncAdaptedQueuePool` (connection pooling for concurrency)

Auto-migration is dialect-aware:
- SQLite: `PRAGMA table_info` → `ALTER TABLE ADD COLUMN`
- Postgres: `information_schema.columns` → `ALTER TABLE ADD COLUMN`

### Row-level locking — `lock_for_update()`

```python
def lock_for_update(query):
    if IS_POSTGRES:
        return query.with_for_update()
    return query  # SQLite: no-op (serialises writes via DB-level locking)
```

Applied to all wallet escrow/settlement operations to prevent concurrent double-spend:

| Operation | File:Line | Lock |
|-----------|-----------|------|
| User→agent escrow | `delegation.py:468` | `from_wallet` |
| Agent→agent escrow | `delegation.py:598` | `delegating_wallet` |
| `/complete` settlement | `delegation.py:985` | both wallets |
| `/callback` settlement | `delegation.py:1172` | both wallets |

### docker-compose.prod.yml

```yaml
postgres:
  image: postgres:16-alpine
  environment:
    - POSTGRES_DB=hive
    - POSTGRES_USER=hive
    - POSTGRES_PASSWORD=${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD}
  healthcheck:
    test: ["CMD-SHELL", "pg_isready -U hive -d hive"]
```

---

## 18. Platform Ed25519 signing (outbound)

Hive generates its own Ed25519 keypair at startup and uses it to sign outbound delegation payloads sent to agents. This replaces the shared `HIVE_SIGNING_SECRET` for the outbound direction.

### Key management — `backend/services/platform_keys.py`

- **Generation**: at first startup, a keypair is generated and persisted in the `platform_keys` table.
- **Env injection**: `HIVE_PLATFORM_PRIVATE_KEY` (PEM) takes precedence — useful for deployments that inject keys via secret files.
- **Publication**: the public key is published at `/.well-known/hive-identity` so agents can verify Hive's signatures.
- **Caching**: the key is loaded once at startup and cached in memory.

### Outbound signing (`services/agent_client.py`)

Every outbound delegation payload now carries **both** signatures (dual mode):
```
X-Hive-Signature: sha256=<hmac>          # legacy (retained during transition)
X-Hive-Signature-Ed25519: <base64>       # preferred
X-Hive-Key-Id: hive-<12hex>
X-Hive-Timestamp: <unix-ts>
```

### Agent runtime verification (`docker/agent_app/main.py`)

The agent runtime:
1. Fetches Hive's public key from `/.well-known/hive-identity` (cached for 5 min).
2. Tries Ed25519 verification first (preferred).
3. Falls back to legacy HMAC if Ed25519 fails or no platform key is available.
4. Fails closed (401) when `HIVE_SIGNING_SECRET` is configured and no signature verifies.

### Well-known identity endpoint

```
GET /.well-known/hive-identity
→ {
    "platform": "Hive Marketplace",
    "key_id": "hive-18fb30597437",
    "public_key_pem": "-----BEGIN PUBLIC KEY-----\n...",
    "signature_algorithm": "Ed25519",
    "legacy_hmac": { "status": "deprecated — retained for dual-signing transition" }
  }
```

---

## 19. Agent-to-agent authentication

When Agent A delegates to Agent B, B can now cryptographically verify who initiated the delegation.

### Delegating agent identity in payloads

The outbound delegation payload now includes a `delegating_agent` field:
```json
{
  "delegation_id": "...",
  "task": "...",
  "delegating_agent": {
    "agent_id": "uuid",
    "name": "Agent A",
    "slug": "agent-a-a1b2c3",
    "signing_key_id": "ak-a1b2c3d4e5f6"
  }
}
```

### Agent runtime — provenance logging

The agent runtime logs the delegating agent's identity on receipt:
```python
if delegating_agent:
    _log_activity("delegation",
        f"Task from {delegating_agent['name']} "
        f"(agent_id={delegating_agent['agent_id'][:8]}, "
        f"key_id={delegating_agent['signing_key_id']})")
```

### SDK — signed delegation requests

Agents can sign their delegation requests with their Ed25519 private key:
```python
client.sign_delegation_request(
    target_agent_id="uuid",
    task_description="do something",
    max_tokens=100,
)
```

The signature is sent in `X-Agent-Signature-Ed25519` + `X-Agent-Key-Id` headers, allowing Hive to verify the delegating agent's identity cryptographically (in addition to the API key).

---

## 20. W3C Decentralized Identifiers (DIDs)

Every agent registered in Hive automatically gets a DID: `did:hive:{agent_id}`.

### DID method — `backend/services/did.py`

- **Method name**: `hive`
- **Method-specific ID**: the agent's UUID
- **Resolution**: `GET /.well-known/did/{did}` or `GET /api/agents/{id}/did-document`
- **DID Document**: follows W3C DID Core spec with Ed25519VerificationKey2020

### DID Document structure

```json
{
  "@context": ["https://www.w3.org/ns/did/v1", "...ed25519-2020/v1"],
  "id": "did:hive:afc5ba80-...",
  "verificationMethod": [{
    "id": "did:hive:afc5ba80-...#signing-key",
    "type": "Ed25519VerificationKey2020",
    "controller": "did:hive:afc5ba80-...",
    "publicKeyMultibase": "u..."
  }],
  "authentication": ["did:hive:...#signing-key"],
  "assertionMethod": ["did:hive:...#signing-key"],
  "service": [
    {"id": "...#delegation", "type": "HiveDelegationEndpoint", "serviceEndpoint": "..."},
    {"id": "...#marketplace", "type": "HiveMarketplaceEntry", "serviceEndpoint": "..."},
    {"id": "...#dashboard", "type": "HiveDashboard", "serviceEndpoint": "..."}
  ],
  "x-hive": { "agent_id": "...", "name": "...", "signing_key_id": "ak-..." }
}
```

### AgentCard integration

The A2A AgentCard now includes the agent's DID:
```json
{ "id": "did:hive:afc5ba80-...", "x-hive": { "did": "did:hive:afc5ba80-..." } }
```

---

## 21. Verifiable Credentials

Hive, as the platform authority, issues W3C Verifiable Credentials (VCs) attesting to agent properties. These are signed with Hive's platform Ed25519 key and verifiable by any third party.

### Credential types — `backend/services/credentials.py`

| Credential | Attests to |
|------------|-----------|
| `HiveOwnershipCredential` | A specific user owns this agent (email, name, registration date) |
| `HiveAgentCredential` | Agent metadata (type, capabilities, status, version, has crypto identity) |
| `HiveMarketplaceCredential` | Marketplace listing (public visibility, pricing model) |

### Endpoints

| Endpoint | Auth | Purpose |
|----------|------|---------|
| `GET /api/agents/{id}/credentials` | Public | All applicable VCs for the agent |

### VC structure

```json
{
  "@context": ["https://www.w3.org/ns/credentials/v2", "...ed25519-2020/v1"],
  "id": "urn:uuid:...",
  "type": ["VerifiableCredential", "HiveOwnershipCredential"],
  "issuer": "did:hive:platform",
  "issuanceDate": "2026-01-01T00:00:00Z",
  "credentialSubject": { "id": "did:hive:...", "agent_name": "...", "owner_email": "..." },
  "proof": {
    "type": "Ed25519Signature2020",
    "verificationMethod": "did:hive:platform#hive-...",
    "proofValue": "<base64-signature>",
    "proofPurpose": "assertionMethod"
  }
}
```

### Verification

Third parties verify a VC by:
1. Fetching Hive's public key from `/.well-known/hive-identity`.
2. Checking the `proof.proofValue` (Ed25519 signature over the VC without the proof block).

The `services.credentials.verify_vc()` function does this programmatically.

---

## 22. mTLS support (optional)

Optional mutual TLS for transport-level cryptographic identity.

### Configuration — `backend/services/mtls.py`

| Env var | Purpose |
|---------|---------|
| `MTLS_ENABLED` | Set to `1` to enable mTLS client cert verification |
| `MTLS_CA_CERT_PATH` | Path to the CA cert that signs agent client certs |

### Agent cert fingerprints

Agents register their client cert fingerprint (SHA-256) on their Agent row:

| Endpoint | Auth | Purpose |
|----------|------|---------|
| `PUT /api/agent/mtls-cert` | Master key | Register/update cert fingerprint |
| `DELETE /api/agent/mtls-cert` | Master key | Remove cert fingerprint |

New Agent column: `mtls_cert_fingerprint` (String(64), indexed).

### How it works

When mTLS is enabled:
1. nginx/Traefik is configured with `ssl_verify_client optional`.
2. The client cert fingerprint is forwarded as `X-Client-Cert-Fingerprint` header.
3. Hive looks up the agent by fingerprint (no API key needed for transport auth).
4. Agents without a cert fall back to API-key auth.

The `get_mtls_nginx_config()` helper returns the nginx config snippet for deployment scripts.

### Additive, not exclusive

mTLS is **additive** — it provides transport-level identity in addition to (not instead of) the application-level API key + Ed25519 signing. Agents can still use API keys when mTLS isn't available (e.g. behind a TLS-terminating proxy).

---

## 23. HMAC deprecation path

### Migration tool — `backend/services/migrate_keys.py`

Issues Ed25519 keypairs to legacy agents (those with `signing_key_id = NULL`):

```bash
# Dry run — show agents without keys
python -m services.migrate_keys

# Apply — issue keys + write private keys to JSON file
python -m services.migrate_keys --apply

# Single agent
python -m services.migrate_keys --agent-id <uuid>
```

The output file (`key_migration_output.json`) contains private keys for out-of-band delivery to agent owners.

### Migration status API

```
GET /api/agents/admin/migration-status  (admin only)
→ {
    "total_agents": 42,
    "migrated": 40,
    "legacy_hmac_only": 2,
    "migration_percentage": 95.2,
    "can_deprecate_hmac": false,
    "message": "2 agent(s) still rely on legacy HMAC. Run the migration tool."
  }
```

### Deprecation timeline

1. **Current**: dual mode — both Ed25519 and HMAC accepted. New agents get Ed25519 keys at registration.
2. **Migrate**: run the migration tool to issue keys to legacy agents.
3. **Verify**: check migration status endpoint — `can_deprecate_hmac` should be `true`.
4. **Deprecate**: remove the HMAC fallback path in `_verify_callback_signature` and the agent runtime's `_verify_hive_signature`.
5. **Remove**: delete `HIVE_SIGNING_SECRET` from env vars and docker-compose.

---

## 24. Security properties (updated)

| Property | Mechanism | Status |
|----------|-----------|--------|
| Agent → Hive auth | bcrypt-hashed API key + prefix index | ✅ |
| Scoped access | `AgentApiKey` table + `require_scopes()` | ✅ |
| Per-agent non-repudiation | Ed25519 keypair, private key with owner | ✅ |
| Platform → agent non-repudiation | Platform Ed25519 keypair, published at `/.well-known/hive-identity` | ✅ |
| Callback integrity | Ed25519 signature over `timestamp.body` | ✅ |
| Callback replay prevention | Redis nonce store + 5-min timestamp window | ✅ |
| Cross-agent forgery prevention | Per-agent keys (no shared secret) | ✅ |
| JWT revocation | `jti` denylist in Redis | ✅ |
| Refresh-token reuse detection | Rotation revokes old `jti` | ✅ |
| Distributed rate limiting | slowapi → Redis | ✅ |
| Agent runtime inbound verification | Ed25519 (preferred) + HMAC (legacy) on `/delegate` | ✅ |
| Raw key isolation | Dedicated `api_key_encrypted` column | ✅ |
| Prod config enforcement | `enforce_prod_config()` at startup | ✅ |
| Backward compatibility | Dual Ed25519 + HMAC verification | ✅ |
| **Portable identity** | W3C DID (`did:hive:{agent_id}`) | ✅ New |
| **Verifiable attestations** | W3C Verifiable Credentials (ownership, agent, marketplace) | ✅ New |
| **Transport-level auth** | Optional mTLS client cert verification | ✅ New |
| **Agent-to-agent provenance** | Delegating agent identity in payload + SDK signing | ✅ New |
| **HMAC deprecation path** | Migration tool + admin status endpoint | ✅ New |
| **Postgres row-level locking** | `SELECT FOR UPDATE` on wallet operations | ✅ New |
