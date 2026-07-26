# LLD 11 — Testing

Hive has two test layers: **Python stdlib-only E2E harnesses** (backend-focused, loopable) and **Playwright browser suites** (UI + backend). Both fund wallets by writing directly to SQLite (bypassing the API) to avoid rate limits.

## Playwright config (`playwright.config.js`)

- `testDir: './tests/playwright'`.
- `fullyParallel: false`, `workers: 1` — serial execution (spec files share state via module-level vars).
- `forbidOnly: !!process.env.CI`, `retries: process.env.CI ? 2 : 0`.
- `reporter: [['list']]`, `timeout: 120000` (2 min).
- `use`: `baseURL = HIVE_BASE` (env, default `http://localhost:8000`), `trace: 'on-first-retry'`, `screenshot: 'only-on-failure'`, `actionTimeout`/`navigationTimeout: 30000`.
- Single project: chromium Desktop Chrome.

## `package.json`

- `name: "hive-e2e-tests"`.
- Scripts: `test:playwright` (`npx playwright test`), `test:playwright:ui`, `test:workflow`, `test:workflow:headed`.
- devDependencies: `@playwright/test ^1.61.0`, `uuid ^10.0.0`.

There's a separate `package.json` at `.opencode/` (opencode tooling tree) — distinct from the test package.

## Python E2E harnesses (`tests/`)

### `tests/e2e_harness.py` (369 lines) — primary backend E2E
Dependency-light (stdlib `urllib` only), designed to be looped. `Harness` dataclass holds state. `run()` executes an 11-step scenario:

1. Register + login.
2. Skills list.
3. Hosted BYOK deploy `POST /api/agents/deploy-hosted` with `model_key`, `skill_names`, `mcp_servers`.
4. Endpoint assigned + status active.
5. `_wait_for_agent()` polls `/health?token=x` until 200.
6. `_check_dashboard()` verifies `/a/{slug}/` returns the real agent app (not login page) — checks for `/status`, `chatInput`, `Alpine` markers.
7. Invoke `POST {endpoint_url}`.
8. OpenClaw deploy (`/api/agents/deploy-openclaw`) — warn-only.
9. Settings key save `PATCH /api/me/keys`.
10. `_grant_tokens()` funds wallet directly via SQLite (multi-path DB discovery).
11. `_check_delegation()` — agent-to-agent delegation via `X-API-Key`, polls `/api/delegate/user-delegations` for `task_result`.
12. `_check_team_delegation()` — create team, `POST /api/teams/{id}/run`, poll to completion, verify delegation tree.
13. Frontend assets served: `/css/theme.css`, `/js/nav.js`, `/deploy`, `/login`, `/signup`.
14. Cleanup: `DELETE /api/agents/{id}`.

`main()` supports `--base` and `--loop` (retry until all pass), exits non-zero on failure. See the `.opencode/skills/hive-e2e/SKILL.md` skill for run instructions.

### `tests/test_workflow_e2e.py` (491 lines) — workflow-specific
Similar stdlib-only structure, `WorkflowHarness` dataclass, 8 phases: auth+fund, deploy agent, workflow CRUD, steps (3 steps with `{{workflow_input.query}}`/`{{prev_output}}` placeholders), run (poll to completion, verify `output_data` + 3 `step_runs`), error cases (draft run → 400, empty name → 422, nonexistent → 404, bad agent → 404, bad token → 401/403), frontend assets, cleanup. Supports `--base` / `--loop`.

### Other Python harnesses
- `tests/test_byoa_e2e.py` (28 KB) — BYOA harness exercising **all three frameworks** (openclaw/langchain/crewai): MCP CRUD, skill attach, deploy, dashboard proxy, LLM invoke, MCP grant/revoke, agent config.
- `tests/test_mcp_e2e.py` (19 KB) — MCP integration: spins up local MCP server (port 9098), backend on 8099, agent on 9000; verifies tool discovery, invocation, revoke-after-restart.

## Playwright suites (`tests/playwright/`)

### `helpers.js` (123 lines) — shared utilities
- Loads `.env` from project root so `OPENROUTER_API_KEY` is available.
- `registerAndLogin(request)` — creates `pw_<uuid>@example.com`, password `PwTest123!`, returns `{email, password, token, uname}`.
- `deployAgent(request, token, name)` — POSTs `/api/agents/deploy-hosted` (framework `openclaw`, skills `['terminal','web_extract']`).
- `grantTokens(email, amount)` — shells out to `grant_tokens.py` via `execSync`.
- `waitForAgent(request, token, agentId, timeout)` — polls `/api/agents/{id}` until active/idle.
- `getUserAgents(request, token)` — lists agents as fallback.

### `team.spec.js` (600 lines)
Two serial suites:
- **"Team Backend Harness — CRUD + Run"** (14 tests): register, deploy 3 agents (with retry/fallback), wait, fund, list (empty), create team (3 members, roles lead/senior/junior), get detail, list, update, run, get run detail, list runs, bad-agent → 404, delete → 204.
- **"Team UI — List, Detail, Org Chart, Run Modal"**: `/teams` page loads, team cards (`[data-testid]`), New Team button, navigation, detail page, org chart hierarchy indentation, run modal (open, textarea, confirm disabled/enabled), sidebar Teams link, empty state, cleanup.

### `team-delegation.spec.js` (215 lines)
Deploys root + worker, creates 2-member team, two scenarios:
- **Self-handle**: task "What is 1+1?" — agent handles itself, output contains "2".
- **A2A delegation**: task asks root to delegate to worker — polls `delegation_tree`, verifies ≥2 delegations and worker delegation completed.

### `team-comprehensive.spec.js` (38 KB)
Most extensive. Creates 3 users (A/B/C) each with multiple agents and 100k tokens. Test groups:
- **A-series**: cross-user isolation (B can't read/update/delete/run A's team).
- **V-series**: validation (nonexistent root, member not owned, 404s, empty name/task).
- **L-series**: lifecycle (hierarchy, field verification, update, list, delete).
- **R-series**: run lifecycle, run detail delegations, list runs, wrong-team/nonexistent run → 404.
- **UI-AUTH / UL / UD / UM**: UI auth redirects, teams list, detail, modal.

### `workflow.spec.js` (377 lines)
24 tests: register, deploy, wait, fund, workflow CRUD, add 3 steps, verify step count, update step, delete step, error cases, then UI tests (login, `/workflows`, `/workflows/new` builder, agent palette, full create-workflow-with-steps flow using Alpine selectors).

### `workflow-paperclip.spec.js` (40 KB)
Two serial suites:
- **"Backend Harness — Workflow CRUD + Run Lifecycle"** (B01-B91): two agents, inline-step workflow, step CRUD, draft/empty-step run → 400, 404/401 cases, run lifecycle, step_run settling, second-run count, cleanup.
- **"Paperclip UI"** (U01-U91): visual pipeline builder — agent cards, flow arrows, step nodes, task textareas, tokens/timeout inputs, remove button, animated delegation flow arrows, empty pipeline dashed state, create-via-UI, edit page, status dropdown, workflows list, run modal with pipeline progress bar, step detail cards, live output, run history cards with progress dots + token counts, legend.

### `create_user.py` (40 lines)
Creates a user directly in SQLite with **bcrypt** hash + 10000-token wallet, bypassing API rate limits. Retries up to 10 times on `sqlite3.OperationalError: locked`. CLI: `create_user.py <name> <email> <password>`. **Hardcoded macOS DB path** (`/Users/rshetty/hive/backend/agent_marketplace.db`) — portability concern.

### `grant_tokens.py` (42 lines)
Funds a wallet by email. Multi-path DB discovery (including `/opt/hive/data/...`). `UPDATE wallets ... balance = balance + ?` or INSERT if no row. CLI: `grant_tokens.py <email> [amount]`.

## CI gating (`.github/workflows/ci.yml`)

Deploy depends on `secret-scan` + `dependency-audit` + `hardening-check` (not CodeQL, which runs independently). See [HLD/07](../HLD/07-deployment.md#cicd-pipeline).

## Known test issues

- **Hardcoded macOS paths** in `create_user.py` (`:6`) and `team-comprehensive.spec.js` (`:21-31`) — break on Linux/CI. `grant_tokens.py` and `e2e_harness.py` use multi-path discovery and are portable.
- **Serial execution** (`workers: 1`) because spec files share state via module-level variables.
- **Direct SQLite wallet funding** bypasses the API — tests need write access to the DB file, which means they must run on the same host as the backend (not against a remote prod instance).
