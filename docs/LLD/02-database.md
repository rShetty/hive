# LLD 02 — Database & Models

Async SQLAlchemy 2.0. Default backend is SQLite (`aiosqlite`); Postgres (`asyncpg`) is supported via `DATABASE_URL`.

## Engine & sessions (`backend/database.py`)

- `DATABASE_URL` env, default `sqlite+aiosqlite:///./agent_marketplace.db` (`database.py:8`).
- Async engine with `NullPool` (`database.py:10-14`).
- `async_session_maker` → `AsyncSession`, `expire_on_commit=False` (`database.py:16`).
- `get_db` dependency (`database.py:25`) yields a session, commits on success, rolls back on exception.
- `Base = declarative_base()` (`database.py:22`).

## Auto-migration (`database.py:38`)

`init_db()`:
1. `Base.metadata.create_all` — creates missing **tables** only (does NOT add columns to existing tables).
2. `_add_missing_columns` (`database.py:51`) — introspects each table via `PRAGMA table_info`, runs `ALTER TABLE ADD COLUMN` for any model column that's missing (skips callable defaults).

This replaces Alembic for dev. The `backend/migrations/` dir holds historical manual SQL/Python scripts (`001_add_marketplace_fields.sql`, `002_add_missing_columns.sql`, `003_add_agent_config.py`) — see `migrations/README.md` for prod Alembic/Postgres guidance.

## Conventions

- All PKs are `String(36)` UUIDs via `str(uuid.uuid4())`.
- `DateTime default=datetime.utcnow`.
- SQLite stores JSON as TEXT.
- Cascade deletes configured per-relationship (e.g. `Agent.skills` cascade delete-orphan).

## Models (`backend/models/`)

### User (`user.py:9`)
`id`, `email` (unique, indexed), `hashed_password`, `name`, `model_api_keys_encrypted: Text` (Fernet JSON `{provider: key}`), `is_active`, `is_admin`, `created_at`. Relationships: `agents`, `wallet` (uselist=False), `workflows`, `teams`.

### Agent (`agent.py:27`)
The central model.
- **Identity**: `id`, `name`, `description`, `slug` (unique indexed, `generate_slug` `:98`), `avatar_url`, `capabilities: JSON(list)`, `tags: JSON(list)`.
- **Marketplace**: `is_public: Boolean`, `marketplace_description: Text`, `pricing_model: JSON({"type":"free"|"token","rate":n})`.
- **Type**: `agent_type` — managed | external | openclaw (`AgentType` enum `:11`).
- **Config**: `config_encrypted: Text` (Fernet — LLM keys, MCP, framework), `openclaw_instance_id: String(36)`.
- **Owner**: `owner_id → users.id` (nullable=False) + `owner`.
- **Auth**: `api_key_prefix: String(16)` indexed, `api_key_hash` (bcrypt).
- **Status**: `status` (pending|verifying|active|idle|offline|error; `AgentStatus` `:18`), `last_seen`, `last_health_check`, `health_check_token`, `ready: Boolean`.
- **Endpoint**: `endpoint_url`, `internal_port: Integer`, `container_id`, `version`.
- **Relationships**: `skills` (AgentSkill cascade), `mcp_access` (AgentMCPAccess cascade).
- `calculate_status()` (`:106`): ERROR sticky; no `last_seen`→PENDING/OFFLINE; <5min→ACTIVE; <30min→IDLE; else OFFLINE.

### Skill (`skill.py:8`)
`id`, `name` (unique indexed, machine name), `display_name`, `description`, `tier` (core|connected|premium), `category`, `required_env_vars: JSON(list)`, `is_active` (stored as string "true"/"false" for SQLite). User-created: `source` (core|user), `visibility` (platform|private), `owner_id`, `definition: JSON` (`{"kind":"prompt"|"tool"|"both", ...}`).

### AgentSkill (`agent_skill.py:9`)
Junction: `agent_id`, `skill_id`, `config: JSON` (encrypted env vars per skill), `added_at`.

### AgentInvite (`agent_invite.py:9`)
`user_id`, `invite_token` (unique indexed), `agent_name`, `agent_type` (default BYOA_CUSTOM), `status` (pending|used|expired), `expires_at`, `used_at`, `agent_id`.

### Wallet (`wallet.py:9`)
`id`, `user_id` (unique FK), `balance: Numeric(10,2)` default 100.00, timestamps.

### Transaction (`transaction.py:24`)
**The canonical ledger + delegation chain.**
- `from_wallet_id`, `to_wallet_id`, `amount: Numeric(10,2)`, `platform_fee: Numeric(10,4)`, `transaction_type` (delegation|payment|refund|admin_grant; `TransactionType` `:10`).
- **Chain**: `delegating_agent_id`, `executing_agent_id`, `originating_user_id`, `session_id` (indexed, groups multi-turn), `delegation_depth` (0 = direct human request).
- `task_description`, `task_result: JSON`, `status` (pending|completed|failed|refunded; `TransactionStatus` `:17`), `refund_reason`, `created_at`, `completed_at`.
- Relationships to both wallets and both agents.

### AgentReview (`agent_review.py:9`)
`agent_id`, `reviewer_user_id`, `delegation_id → transactions.id`, `rating: int (1-5)`, `comment`. `UniqueConstraint('delegation_id', 'uq_delegation_review')` — one review per delegation.

### DelegationLog (`delegation_log.py:13`)
Persisted SSE event history: `delegation_id → transactions.id` (indexed), `timestamp`, `level`, `message`, `data: JSON`, `source` (system|agent). Composite index `(delegation_id, timestamp)`. `to_event()` (`:33`) returns frontend SSE shape.

### MCPServer (`mcp.py:13`) + AgentMCPAccess (`:70`)
- **MCPServer**: `owner_id`, `name`, `url`, `description`, `transport` (http|sse|stdio), `command` (stdio), `env_encrypted`, `headers_encrypted`, `auth_type` (headers|oauth), `oauth_encrypted` (access/refresh/expiry/client_id/secret/issuer/scope), `oauth_client_id/secret`, `oauth_scopes`, `visibility` (private|platform), `is_active`, timestamps. `access_grants` cascade.
- **AgentMCPAccess**: `agent_id`, `mcp_server_id`, `headers_encrypted` (per-agent override), `enabled`, `created_at`.

### Workflow family (`workflow.py`)
- Enums: `WorkflowStatus` (draft|active|archived), `WorkflowRunStatus` (pending|running|completed|failed|cancelled), `StepRunStatus` (pending|running|completed|failed|skipped).
- **Workflow** (`:32`): `name`, `description`, `owner_id`, `status`, `max_tokens_per_run=500`, `timeout_seconds=600`, `auto_retry`, `max_retries=2`, timestamps. Relationships: `steps` (ordered by `step_order`, cascade), `runs` (cascade).
- **WorkflowStep** (`:61`): `workflow_id`, `agent_id`, `name`, `description`, `step_order`, `task_template: Text` (`{{prev_output}}` placeholders), `max_tokens`, `timeout_seconds`, `input_mapping: JSON`, `condition: JSON` (e.g. `{"skip_if": ...}`).
- **WorkflowRun** (`:96`): `workflow_id`, `user_id`, `status`, `input_data`, `config_overrides`, `output_data`, `error_message`, `total_tokens_used`, timing. `step_runs` ordered by `created_at`.
- **WorkflowStepRun** (`:131`): `workflow_run_id`, `workflow_step_id`, `agent_id`, `delegation_id → transactions.id`, `status`, `step_order`, `input_data`, `output_data`, `error_message`, `tokens_used`, timing.

### Team family (`team.py`)
- Enums: `TeamRunStatus`, `TeamDelegationStatus` (pending|running|completed|failed).
- **Team** (`:24`): `name`, `description`, `owner_id`, `root_agent_id`, `max_depth=3`, timestamps. Relationships: `members` (cascade), `runs` (cascade), `root_agent`.
- **TeamMember** (`:47`): `team_id`, `agent_id`, `role` (default "member"), `reports_to_member_id` (self-FK → hierarchical tree), `max_tokens=200`. `reports_to` relationship with `direct_reports` backref.
- **TeamRun** (`:68`): `team_id`, `user_id`, `task`, `status`, `delegation_tree: JSON`, `total_tokens_used`, `output_data`, `error_message`, timing. `delegations` cascade.
- **TeamDelegation** (`:95`): `team_run_id`, `parent_delegation_id` (self-FK → tree), `delegation_id → transactions.id`, `agent_id`, `task_description`, `status`, `tokens_used`, `result_data`, `error_message`, `depth`, timing. `parent`/`children` self-referential.

## Entity relationships (summary)

```
User ──< Agent ──< AgentSkill >── Skill
  │        │        │
  │        └──< AgentMCPAccess >── MCPServer
  │
  ├──1 Wallet ──< Transaction >── Wallet
  │                    │
  │                    ├── delegating_agent_id → Agent
  │                    ├── executing_agent_id → Agent
  │                    └── originating_user_id → User
  │
  ├──< Workflow ──< WorkflowStep → Agent
  │        │
  │        └──< WorkflowRun ──< WorkflowStepRun → Transaction (delegation_id)
  │
  └──< Team ──< TeamMember (self-ref tree) → Agent
           │
           └──< TeamRun ──< TeamDelegation (self-ref tree) → Transaction (delegation_id)

Transaction ──< DelegationLog
Transaction ──1 AgentReview
```

The `Transaction.delegation_id` (implicit — the Transaction *is* the delegation) is referenced by `WorkflowStepRun.delegation_id`, `TeamDelegation.delegation_id`, `AgentReview.delegation_id`, and `DelegationLog.delegation_id` — making it the join point across all orchestration features.
