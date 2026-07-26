# HLD 06 — Real-time & Data Flow

Hive uses **Server-Sent Events (SSE)** for all real-time updates — delegation progress, team run tree evolution, and workflow step progress. No WebSockets.

## Why SSE

- **Unidirectional** — server→client is all we need; the client sends commands via normal REST.
- **Auto-reconnect** — the browser `EventSource` reconnects automatically with `Last-Event-ID`; Hive replays missed events from `DelegationLog`.
- **Proxy-friendly** — SSE passes through nginx/Traefik/CDNs cleanly (with `X-Accel-Buffering: no`).
- **Downside** — `EventSource` can't set headers, so auth is via `?token=` query param.

## The delegation hub

`services/delegation_hub.py` is the low-latency fan-out layer: an in-memory `dict[str, list[asyncio.Queue]]`.

```
 publisher (orchestrator)                 subscribers (SSE generators)
        │                                          │
        │  publish(channel, event)                 │ subscribe(channel) → Queue(maxsize=1000)
        │  → put_nowait to each queue              │
        ├─────────────────────────────────────────►│
        │                                          │  await queue.get() → yield SSE event
        │                                          │
```

- `publish` is non-blocking (`put_nowait`); drops on full queue (slow subscriber).
- `subscribe` returns a queue; `unsubscribe` removes it.
- `subscriber_count` for observability.
- `TERMINAL_STATUSES = {"completed", "failed"}`.

The hub is **ephemeral** — events in flight when Hive restarts are lost. Durability is provided by `DelegationLog` (DB), which the SSE generator replays on (re)connect.

## The log funnel

`add_delegation_log` (`routers/delegation.py:93`) does three things atomically per log event:

1. **Publishes** to `delegation_hub` (live subscribers get it instantly).
2. **Mirrors** to the legacy in-process `delegation_logs` dict (used by polling fallbacks and team DB-poll).
3. **Persists** a `DelegationLog` row (so reconnects can replay).

`set_delegation_status` similarly updates the in-process `delegation_status` cache + publishes + logs.

## SSE generator pattern

`_sse_event_generator` (`routers/delegation.py:757`) — used by delegation, with variants in teams/workflows:

1. **Subscribe to the hub FIRST** — so events published during the DB read below land in the queue, not lost.
2. **Replay history** — read `DelegationLog` rows for this delegation, emit them (frontend de-dupes on timestamp).
3. **Emit initial status** — current `delegation_status`.
4. **Tail the queue** — `await asyncio.Queue.get()` with a 15 s timeout; on timeout emit `: heartbeat` comment (keeps connection alive through proxies).
5. **Terminal** — on `completed`/`failed`, emit `done` and return.

## Three streaming endpoints

| Flow | Endpoint | Auth | Channel |
|------|----------|------|---------|
| Delegation | `GET /api/delegate/{id}/user-stream` (`?token=`) or `/stream` (API key) | JWT query / API key | delegation id |
| Team run | `GET /api/teams/{team_id}/runs/{run_id}/stream?token=` | JWT query | `team_{team_run_id}` |
| Workflow run | `GET /api/workflows/{id}/runs/{run_id}/stream?token=` | JWT query | `workflow_{run_id}` |

All set `media_type="text/event-stream"` + `Cache-Control: no-cache` + `Connection: keep-alive` + `X-Accel-Buffering: no`.

## Event contract

Events are JSON objects with a `type` discriminator. The hub publishes `{type, data}`; SSE generators normalize to the frontend's expected flat shape.

- `log` — `{type:"log", agent, level, message}` (level: thinking/action/info/warning/success/error).
- `status` / `status_update` — `{type:"status_update", status, team_run_id}`.
- `step_update` (workflows) — step progress.
- `team_run_started` / `team_run_finished` — `{type:"team_run_finished", status, team_run_id}`.
- `done` — terminal sentinel.

**Normalization** (`teams.py:1035`): hub log events arrive as `{type:"log", data:{level, message}}`; the SSE generator unwraps `data` into top-level fields. Status events are rewritten to `status_update`. This was the subject of a recent bugfix (commit `7f413ee`) where local shadowing of `delegation_logs` swallowed logs — see [LLD/06](../LLD/06-teams.md#known-issues).

## Team run streaming specifics

Team runs are the most complex stream because they span multiple delegations (each with its own hub channel) plus an evolving tree. `stream_team_run_events` (`teams.py:995`):

1. Emit initial state (team, run, root delegation).
2. Emit existing delegations + tree + output (if already terminal).
3. Replay logs.
4. If terminal → emit finished, return.
5. Else: subscribe to hub **and** poll DB every ~2 s (0.5 s loop, max 480 polls ≈ 4 min) for delegation list / log / tree changes. The DB poll is the secondary sync that reconciles cross-channel state.

## Frontend SSE consumption

Three pages consume SSE via `new EventSource(url + '?token=...')`:
- `team-detail.html:497` — team run stream.
- `workflow-builder.html:800` — workflow run stream.
- `tasks.html:489` — delegation stream.

Each listens for `message` events, parses JSON, and updates the DOM (Alpine reactivity or direct DOM manipulation). Reconnect is automatic; the replay path handles missed events.

## Data flow: full delegation (end to end)

```
1. Browser ──POST /api/delegate/user-request──► Hive (escrow, create Transaction depth 0, seed logs)
2. Hive ──background task──► AgentClient.send_delegation_task ──POST /delegate (HMAC)──► Agent
3. Browser ──GET /user-stream?token=──► Hive SSE generator (subscribe hub, replay DelegationLog, tail queue)
4. Agent ──POST /progress──► Hive (add_delegation_log → hub + dict + DB) ──► SSE ──► Browser
5. Agent ──POST /complete {result, tokens_used}──► Hive (_settle_delegation: fee + refund, tx COMPLETED)
6. Hive ──publish status:completed──► SSE ──► Browser (emit "done", close)
```

## Data flow: team run

```
1. Browser ──POST /api/teams/{id}/run──► Hive (escrow max_depth*200, create root tx + TeamRun + root TeamDelegation)
2. Hive ──background _run_team_delegation──► ping root /health
3. Hive ──POST root /invoke (plan prompt)──► root agent ──► JSON plan [{agent_id, task}]
4. Hive: for each sub-task, create Transaction (depth 1) + TeamDelegation; asyncio.gather over send_delegation_task(sync=True)
5. Hive ──POST root /invoke (synthesize)──► root agent ──► final output
6. Hive: build delegation tree, mark run/tx completed, publish status:completed
7. Browser ──GET /stream?token=──► SSE (tree + logs + status, with DB poll secondary sync)
```
