# LLD 06 — Teams

`backend/routers/teams.py` (1234 lines) implements hierarchical multi-agent orchestration: a root agent plans sub-delegations, Hive executes them concurrently, then the root synthesizes.

## Data model (`models/team.py`)

- **Team** (`:24`): `name`, `description`, `owner_id`, `root_agent_id`, `max_depth=3`. Relationships: `members`, `runs`, `root_agent`.
- **TeamMember** (`:47`): `team_id`, `agent_id`, `role` (default "member"), `reports_to_member_id` (self-FK → hierarchical tree), `max_tokens=200`. `reports_to`/`direct_reports`.
- **TeamRun** (`:68`): `team_id`, `user_id`, `task`, `status`, `delegation_tree: JSON`, `total_tokens_used`, `output_data`, `error_message`, timing. `delegations` cascade.
- **TeamDelegation** (`:95`): `team_run_id`, `parent_delegation_id` (self-FK → tree), `delegation_id → transactions.id`, `agent_id`, `task_description`, `status`, `tokens_used`, `result_data`, `error_message`, `depth`, timing. `parent`/`children` self-referential.

## Rate limits (`TEAM_RATE_LIMITS`, `teams.py:65`)

list/create/detail/update/delete 600/h, run 30/h, stream 120/h.

## CRUD

GET `/` (list), POST `/` (`TeamCreate` with members + `reports_to` graph), GET/PATCH/DELETE `/{id}`. Validation: members must be owned by the user, root must exist and be active, name non-empty.

## Run — `POST /{team_id}/run` (`:871`)

`TeamRunCreate{task}`.

1. **Validate** root + members are active/idle (`_check_agent_alive` `:29` pings `/health`).
2. **Escrow** `max_depth * 200` tokens from user → root wallet.
3. Create root `Transaction` (depth 0) + `TeamRun` + root `TeamDelegation`.
4. Seed `delegation_status` / logs.
5. Publish `team_run_started` to hub.
6. `background_tasks.add_task(_run_team_delegation, ...)`.

## Orchestrator — `_run_team_delegation` (`:433`)

Server-side background task:

### 1. Plan
- POST root agent `/invoke` with a **plan prompt** asking for a JSON list of delegations `[{"agent_id","task"}]`.

### 2. Parse the plan
Accepts three formats:
- JSON array (preferred).
- Pseudo tool-call regex `<|tool_call>...<tool_call|>`.
- Freeform `target_agent_id` / `task_description` extraction.

### 3. Branch
- **No delegations** → run sync on root, complete.
- **Has delegations** → fan-out (below).

### 4. Fan-out
For each sub-delegation:
- Create a real `Transaction` (depth 1, parent=root).
- Create a `TeamDelegation` row.
- Run all sub-tasks **concurrently** via `client.send_delegation_task(..., sync=True)` with `asyncio.gather`.

### 5. Synthesize
- POST root `/invoke` with compiled sub-results → final output.

### 6. Finalize
- Build delegation tree dict (recursive parent/children → nested structure).
- Mark run/tx COMPLETED.
- Publish `status: completed` to hub.

### Failure — `_fail_team_run` (`:407`)
Marks failure + publishes `status: failed`.

## SSE stream — `stream_team_run_events` (`:995`)

`GET /{team_id}/runs/{run_id}/stream?token=` (JWT query auth).

The most complex stream in Hive because it spans multiple delegations plus an evolving tree:

1. Emit initial state (team, run, root delegation).
2. Emit existing delegations + tree + output (if already terminal).
3. **Replay logs** — from `delegation_logs` dict (imported from `delegation.py`; **not** redefined locally — see "Known issues").
4. If terminal → emit `team_run_finished`, return.
5. Else: subscribe to hub **and** poll DB every ~2 s (0.5 s loop, max 480 polls ≈ 4 min) for:
   - delegation list changes,
   - new log entries,
   - tree updates,
   - terminal status.

### Event normalization (`teams.py:1035`)
Hub log events arrive as `{type:"log", data:{level, message}}`; the SSE generator unwraps `data` into top-level fields to match the frontend's expected flat shape. Status events are rewritten to `status_update`. A `log` event is also emitted with the `agent` name resolved from `root_td.agent.name`.

## Known issues / recent fixes

### Log shadowing (commit `7f413ee`)
**Root cause**: `teams.py` previously redefined `delegation_status`, `delegation_logs`, and `add_delegation_log` locally (lines ~956-962). The local `add_delegation_log` only appended to the local empty dict — never published to `delegation_hub` or persisted to DB. Meanwhile the agent's progress messages went to `delegation.py`'s `add_delegation_log` (hub + DB), but the SSE stream read from the wrong (local empty) dict. Result: live logs didn't show in team run SSE.

**Fix**: removed the local shadowing; `teams.py` now imports `delegation_logs`, `delegation_status`, `add_delegation_log` from `routers.delegation`. A comment at the redefinition site (`:953-954`) prevents regression.

### Hub event format mismatch
Hub events have format `{type:'log', data:{level, message}}` but the SSE stream and frontend expected `{type:'log', level, message}`. Fixed by adding normalization in the hub event draining loop.

### Other notes
- The team orchestrator uses `sync=True` for sub-delegations, so the `/complete` callback reconciliation path in `delegation.py` is mostly a fallback for async-mode teams (not currently used).
- `tokens_used=1.0` is hardcoded in the agent runtime's sync delegation path — a placeholder that should eventually reflect real usage.
- The root agent's plan quality depends on the framework: only `main.py` (openclaw) injects the **team context block** into the system prompt; crewai/langchain variants do not (see [LLD/04](04-agent-runtime.md#known-inconsistencies)).
