# LLD 07 — Workflows

`backend/routers/workflows.py` (1190 lines) implements deterministic sequential pipelines: a saved sequence of steps, each calling an agent with a templated task.

## Data model (`models/workflow.py`)

- **Enums**: `WorkflowStatus` (draft|active|archived), `WorkflowRunStatus` (pending|running|completed|failed|cancelled), `StepRunStatus` (pending|running|completed|failed|skipped).
- **Workflow** (`:32`): `name`, `description`, `owner_id`, `status`, `max_tokens_per_run=500`, `timeout_seconds=600`, `auto_retry`, `max_retries=2`, timestamps. Relationships: `steps` (ordered by `step_order`, cascade), `runs` (cascade).
- **WorkflowStep** (`:61`): `workflow_id`, `agent_id`, `name`, `description`, `step_order`, `task_template: Text` (`{{prev_output}}` placeholders), `max_tokens`, `timeout_seconds`, `input_mapping: JSON`, `condition: JSON` (e.g. `{"skip_if": ...}`).
- **WorkflowRun** (`:96`): `workflow_id`, `user_id`, `status`, `input_data`, `config_overrides`, `output_data`, `error_message`, `total_tokens_used`, timing. `step_runs` ordered by `created_at`.
- **WorkflowStepRun** (`:131`): `workflow_run_id`, `workflow_step_id`, `agent_id`, `delegation_id → transactions.id`, `status`, `step_order`, `input_data`, `output_data`, `error_message`, `tokens_used`, timing.

## CRUD

- GET `` — list (status filter, paginated).
- POST `` — create + optional steps.
- GET/PUT/DELETE `/{id}`.
- POST `/{id}/steps` — add step.
- PUT/DELETE `/{id}/steps/{step_id}`.

Validation: draft workflows can't be run (400), empty name → 422, nonexistent workflow/agent → 404, bad token → 401/403.

## Run — `POST /{id}/run` (`:517`-ish)

`WorkflowRunCreate{task}`.

1. Validate workflow is `active`.
2. Create `WorkflowRun` + placeholder `WorkflowStepRun`s.
3. `asyncio.create_task(_execute_workflow_run(...))`.
4. Return run id immediately.

## Executor — `_execute_workflow_run` (`:517`)

Sequential step execution. For each step (in `step_order`):

### 1. Condition check
Evaluate `condition.skip_if` — if met, mark step SKIPPED and continue.

### 2. Template resolution — `_resolve_template`
Resolve `task_template` / `input_mapping` via Jinja-style placeholders:
- `{{workflow_input.query}}` → run input.
- `{{prev_output}}` → previous step's output.
- `{{step_N.output}}` → step N's output.
- `{{key}}` → arbitrary context key.

### 3. Execute
- Create `WorkflowStepRun`.
- **Escrow** tokens from user wallet.
- Create `Transaction` (session_id=run.id, depth=0).
- Call `agent_client.send_delegation_task`.

### 4. Settle
- If sync `completed` → `_settle_delegation` (10% platform fee, refund remainder).
- Else poll `delegation_status` for callback.

### 5. Publish events
Publishes to `delegation_hub` on channel `workflow_{run_id}`:
- `step_update` — step progress.
- `log` — log entries.
- `status` — run status changes.
- `done` — terminal.

### Failure handling
Failed step → refund + break the run (no further steps execute). Run status → FAILED.

## SSE stream

`GET /{id}/runs/{run_id}/stream?token=` (JWT query auth, `workflows.py:1109`).

Replays status then tails the `workflow_{run_id}` hub queue with a 15 s heartbeat + DB terminal re-check. Same `_sse_event_generator` pattern as delegation (see [LLD/05](05-delegation-engine.md#sse-streaming)).

## Frontend

`workflow-builder.html` (56 KB, the largest page) is a visual pipeline builder: agent palette → step cards → save/run. Uses Alpine.js. Loads `/api/agents?limit=100`, `/api/workflows/{id}`, runs; create/update/delete steps; SSE stream at `:800-801`. The "paperclip" UI shows animated delegation flow arrows between steps.

`workflows.html` — list + run trigger.

## Workflows vs Teams

| Aspect | Workflows | Teams |
|--------|-----------|-------|
| Structure | Linear sequence | Hierarchical tree |
| Planning | Fixed at design time | LLM decides at runtime |
| Concurrency | Serial | Sub-delegations concurrent |
| Templating | `{{prev_output}}` / `{{step_N.output}}` | None (LLM writes sub-tasks) |
| Output | Last step's output | Root synthesizes |
| Conditionals | `skip_if` per step | None |
| Retry | `auto_retry` / `max_retries` | None |

Workflows are for **deterministic, repeatable** pipelines (same steps every run, only input varies). Teams are for **adaptive** fan-out where the root agent decides who to delegate to based on the task.
