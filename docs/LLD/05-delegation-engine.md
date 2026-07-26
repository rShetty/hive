# LLD 05 — Delegation Engine

`backend/routers/delegation.py` (1281 lines) is the heart of Hive's work-dispatch system. It handles human→agent and agent→agent delegation, token escrow/settlement, agent callbacks, and SSE streaming.

## Constants (`delegation.py`)

- `MAX_DELEGATION_DEPTH = 5` (`:73`)
- `PLATFORM_FEE_PCT = 0.10` (10% platform fee) (`:70`)
- `HIVE_SIGNING_SECRET` env (default `change-me-in-production`) (`:76`)

## In-process state (module-level)

- `delegation_status: dict` — in-memory status cache per delegation id.
- `delegation_logs: dict` — in-memory log list per delegation id (legacy; also persisted to `DelegationLog`).
- These are imported by `teams.py` — **do not shadow them locally** (commit `7f413ee` fixed a bug where `teams.py` redefined them and swallowed logs).

## Entry points

### `POST /api/delegate/user-request` (`:419`) — human→agent
1. Validate target agent is public/active/ready.
2. **Escrow** `max_tokens` from user wallet (atomic flush + overdraft check → 402).
3. Check pricing min rate.
4. Create `Transaction` (depth 0, `originating_user_id` set).
5. Seed `delegation_status` + first log.
6. `background_tasks.add_task(_execute_delegation_task, ...)`.
7. Return `{delegation_id}` immediately.

### `POST /api/delegate/request` (`:527`) — agent→agent (API-key auth)
1. Resolve delegating agent from `X-API-Key`.
2. Enforce `MAX_DELEGATION_DEPTH=5` by walking the `session_id` chain (`:561-577`).
3. Thread `originating_user_id` from the session root.
4. Allow private agents if same owner.
5. Same escrow/create/background flow.

## Background executor — `_execute_delegation_task` (`:206`)

1. Log dispatch.
2. Call `agent_client.send_delegation_task` (HMAC-signed POST to agent `/delegate`).
3. On `AgentTimeoutError` / `AgentConnectionError` → `_mark_failed` (full refund).
4. If agent returns `status=completed` synchronously → `_settle_from_background`.
5. Else agent accepted (pending) — await callback.

## Settlement — `_settle_delegation` (`:155`)

```
tokens_used = min(reported, escrowed)
agent_wallet += tokens_used - (tokens_used * 0.10)   # 10% platform fee
delegator_wallet += escrowed - tokens_used           # refund remainder
tx.status = COMPLETED
```
Idempotent — no-op if tx is not PENDING.

`_mark_failed` (`:193`) refunds the full escrow, tx → FAILED.

## Log funnel — `add_delegation_log` (`:93`)

Per log event, atomically:
1. **Publish** to `delegation_hub` (live subscribers).
2. **Mirror** to legacy `delegation_logs` dict.
3. **Persist** a `DelegationLog` row (replay-on-reconnect).

`set_delegation_status` (`:140`) updates cache + publishes + logs.

## Callbacks (agent → Hive)

| Endpoint | Line | Auth | Purpose |
|----------|------|------|---------|
| `POST /{id}/progress` | `:902` | API key | `add_delegation_log(source="agent")` with `{level, message, data}` |
| `POST /{id}/complete` | `:936` | API key | settle + mark COMPLETED; reconcile TeamDelegation/TeamRun if part of a team run |
| `POST /{id}/fail` | `:1019` | API key | refund + mark FAILED |
| `POST /{id}/callback` | `:1077` | HMAC signature | async completion with signature verification |

### HMAC verification — `_verify_callback_signature` (`:1061`)
Checks `X-Hive-Signature` (format `sha256={hmac}`) + `X-Hive-Timestamp`, timing-safe `hmac.compare_digest`. This authenticates async completions without an API key in the request.

### `/complete` payload contract
`DelegationComplete` schema: `{result, tokens_used}`. (Commit `b3dc013` fixed the agent runtimes that were sending `{output, agent_id, delegation_id}` and getting 422s.)

### Team reconciliation
`/complete` checks if the delegation is a `TeamDelegation`; if so, updates the tree and may mark the `TeamRun` completed when all sub-delegations finish. This is how async team sub-delegations (non-sync mode) would settle — though the current team orchestrator uses `sync=True`, so this path is a fallback.

## SSE streaming — `_sse_event_generator` (`:757`)

```
1. subscribe to delegation_hub (FIRST, so events during DB read land in queue)
2. replay DelegationLog history (frontend de-dupes on timestamp)
3. emit initial delegation_status
4. tail asyncio.Queue.get() with 15s timeout
     - on event: yield _emit(event); if terminal status → yield done, return
     - on timeout: yield ": heartbeat" comment
```

`_emit` formats an SSE `data: {json}\n\n` frame.

Endpoints:
- `GET /{id}/user-stream` (`:815`) — user, `?token=` JWT auth.
- `GET /{id}/stream` (`:844`) — agent, API key auth.

## Token estimation — `POST /estimate` (`:395`)

`_estimate_task_tokens` (`:364`): heuristic — base 20 + length_bonus (1/10 chars, cap 80) × complexity multiplier (matched keywords from `_COMPLEXITY_KEYWORDS`, up to 1.8×), clamped to agent min rate and [10, 1000].

## Discovery & listing

- `GET /discover` (`:395`-ish) — agent view of public agents by skill/cost/rating.
- `GET /user-delegations` — user's delegations.
- `GET /my-delegations` — agent's delegations.
- `GET /{id}/status`, `/user-status`, `/logs`, `/user-logs` — status/log reads.

## `AgentClient` (`services/agent_client.py`)

`send_delegation_task` (`:43`):
- Builds payload `{delegation_id, task, max_tokens, context, callback_url, requested_at, sync}`.
- Appends `/delegate` to endpoint (strips `/invoke` first); resolves relative endpoints against `MARKETPLACE_URL`.
- HMAC-SHA256 signature (`_make_signature` `:30`) → headers `X-Hive-Signature: sha256=...`, `X-Hive-Timestamp`, `X-Hive-Delegation-ID`.
- Exceptions: `AgentTimeoutError`, `AgentConnectionError`, `AgentClientError`.
- `send_callback` (`:144`) for async completions.
- Singleton `get_agent_client()` (`:197`).

## Data model

Every delegation = one `Transaction` row (`delegation_depth`, `session_id`, `originating_user_id`, `delegating_agent_id`, `executing_agent_id`, `amount` escrowed, `platform_fee`, `status`, `task_result`). `DelegationLog` rows hang off `delegation_id`. `TeamDelegation` and `WorkflowStepRun` reference the same `Transaction` via their `delegation_id` FK.
