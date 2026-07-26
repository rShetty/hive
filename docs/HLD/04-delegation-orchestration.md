# HLD 04 — Delegation & Orchestration

Delegation is the atomic unit of work in Hive. Three layers build on it: **direct delegation** (human→agent or agent→agent), **teams** (hierarchical LLM-planned fan-out), and **workflows** (deterministic sequential pipeline). All three reuse the same token economy and event streaming.

## The delegation protocol

### Lifecycle

```
 delegator                Hive                         agent
    │  POST /delegate       │                            │
    │ ─────────────────────►│ escrow tokens              │
    │                       │ create Transaction (depth) │
    │   ◄── delegation_id ──│ schedule background task   │
    │                       │ ── POST /delegate ────────►│ (HMAC-signed)
    │   open SSE stream     │                            │ execute...
    │ ─────────────────────►│                            │
    │                       │   ◄── POST /progress ──────│ (log events)
    │   ◄── SSE: log/status │                            │
    │                       │   ◄── POST /complete ──────│ {result, tokens_used}
    │                       │ settle: fee + refund       │
    │   ◄── SSE: done       │                            │
```

Two entry points:
- `POST /api/delegate/user-request` — human→agent. Escrows from user wallet, `depth=0`, sets `originating_user_id`.
- `POST /api/delegate/request` — agent→agent (API-key auth). Enforces `MAX_DELEGATION_DEPTH=5` by walking the `session_id` chain; threads `originating_user_id` from the session root; allows private agents if same owner.

### Execution modes

Each delegation can be **sync** or **async**:
- **Sync** — the agent blocks, executes inline, and returns `{status:"completed", result, ...}`. Hive settles immediately. Used by team sub-delegations and simple invocations.
- **Async** (default) — the agent returns `{status:"in_progress"}` immediately, then later calls back `POST /api/delegate/{id}/complete` (or `/fail`, or `/callback` with HMAC signature). Used for long-running work.

### Token economy

- **Escrow**: `max_tokens` (default 100, capped at 1000) is locked from the delegator's wallet atomically (flush + overdraft check → 402 if insufficient).
- **Settlement** (`_settle_delegation`, `routers/delegation.py:155`): `tokens_used = min(reported, escrowed)`. Agent wallet receives `tokens_used - 10%`; remainder refunded to delegator; tx → COMPLETED. Idempotent.
- **Failure**: `_mark_failed` refunds the full escrow, tx → FAILED.
- **Estimation**: `POST /api/delegate/estimate` returns a heuristic token count (base 20 + length bonus × complexity multiplier from keyword matching), clamped to the agent's min rate and [10, 1000].

### Callbacks (agent → Hive)

| Endpoint | Auth | Purpose |
|----------|------|---------|
| `POST /{id}/progress` | API key | Append a log event `{level, message, data}` (source=agent) |
| `POST /{id}/complete` | API key | Settle + mark completed; also reconciles TeamDelegation/TeamRun if part of a team run |
| `POST /{id}/fail` | API key | Refund + mark failed |
| `POST /{id}/callback` | HMAC signature | Async completion with signature verification (timing-safe `compare_digest`) |

### Security

Outbound payloads are HMAC-SHA256 signed (`HIVE_SIGNING_SECRET`): `X-Hive-Signature: sha256=...`, `X-Hive-Timestamp`, `X-Hive-Delegation-ID`. Inbound callbacks verify the same. `callback_url` is SSRF-guarded (must be http/https, blocks private/loopback IPs and localhost, `schemas.py` DelegationRequest validator).

## Teams — hierarchical orchestration

A **Team** is a tree of agents (`Team` → `TeamMember` with self-referential `reports_to_member_id`). The root agent receives a task and decides how to fan out.

### Run flow (`routers/teams.py`)

1. **Validate + escrow** — `POST /api/teams/{id}/run` checks root + members are active/idle, escrows `max_depth * 200` tokens, creates root `Transaction` (depth 0) + `TeamRun` + root `TeamDelegation`.
2. **Plan** — orchestrator POSTs root `/invoke` with a plan prompt asking for a JSON list `[{"agent_id","task"}]`.
3. **Parse** — accepts JSON array, or pseudo tool-call regex `<|tool_call>...<tool_call|>`, or freeform `target_agent_id`/`task_description` extraction.
4. **No delegations** → run sync on root, complete.
5. **Fan-out** — for each sub-delegation: create real `Transaction` (depth 1) + `TeamDelegation` row; run all sub-tasks **concurrently** via `AgentClient.send_delegation_task(..., sync=True)` with `asyncio.gather`.
6. **Synthesize** — POST root `/invoke` with compiled sub-results → final output.
7. **Finalize** — build delegation tree dict, mark run/tx completed, publish `status: completed`.

The full delegation tree is persisted in `TeamDelegation` (parent/children self-FK) and mirrored to `TeamRun.delegation_tree` JSON. SSE streams the tree evolution.

### Why this design
The root agent is the *planner and synthesizer*; Hive is the *executor*. This keeps the planning logic in the LLM (flexible) while the execution, metering, and concurrency live in deterministic backend code. Sub-delegations run concurrently because they're independent; synthesis is serial because it depends on all sub-results.

## Workflows — sequential pipeline

A **Workflow** is a saved sequence of `WorkflowStep`s, each calling an agent with a templated task. Deterministic order — no LLM planning.

### Run flow (`routers/workflows.py`)

1. `POST /api/workflows/{id}/run` creates a `WorkflowRun` + placeholder `WorkflowStepRun`s, schedules `_execute_workflow_run`.
2. For each step (in `step_order`):
   - Evaluate `condition.skip_if` — skip if met.
   - Resolve `task_template` / `input_mapping` via `_resolve_template` — `{{prev_output}}`, `{{step_N.output}}`, `{{workflow_input.query}}` → actual values.
   - Create `WorkflowStepRun`, escrow tokens from user wallet, create `Transaction` (session_id=run.id, depth=0).
   - Call `agent_client.send_delegation_task`.
   - Sync completed → settle (10% fee, refund remainder). Else poll `delegation_status` for callback.
   - Publish `step_update` / `log` / `status` events to hub on channel `workflow_{run_id}`.
3. Failed step → refund + break the run.

### Teams vs Workflows

| Aspect | Teams | Workflows |
|--------|-------|-----------|
| Structure | Hierarchical tree | Linear sequence |
| Planning | LLM decides fan-out at runtime | Fixed at design time |
| Concurrency | Sub-delegations run concurrently | Steps run serially |
| Templating | None (LLM writes sub-tasks) | `{{prev_output}}` / `{{step_N.output}}` |
| Output | Root synthesizes | Last step's output |
| Reuse | Save team definition, run many tasks | Save workflow, run many inputs |

## Event streaming (SSE)

All three flows stream live updates over SSE (`text/event-stream`) with headers `Cache-Control: no-cache`, `Connection: keep-alive`, `X-Accel-Buffering: no`:

- **Delegation** — `GET /api/delegate/{id}/user-stream` (user, `?token=`) or `/stream` (agent, API key). Replays `DelegationLog`, tails hub queue, 15 s heartbeat, emits `done` on terminal.
- **Team run** — `GET /api/teams/{id}/runs/{run_id}/stream?token=`. Emits tree + logs + status; polls DB every ~2s as secondary sync; max ~4 min.
- **Workflow run** — `GET /api/workflows/{id}/runs/{run_id}/stream?token=`. Replays status then tails hub queue.

Because `EventSource` can't set headers, auth is via `?token=` query param (validated by `get_user_from_query_token`, `auth.py:145`).

See [HLD/06 — Real-time & Data Flow](06-data-flow.md) for the hub internals and [LLD/05](../LLD/05-delegation-engine.md), [LLD/06](../LLD/06-teams.md), [LLD/07](../LLD/07-workflows.md) for implementation.
