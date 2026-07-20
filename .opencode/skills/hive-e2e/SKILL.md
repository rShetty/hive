---
name: hive-e2e
description: Use this skill when you need to run, automate, or loop the Hive end-to-end test harness. Trigger when verifying that the Hive backend + hosted BYOK agents + frontend work, or when asked to "run the e2e tests" / "loop the tests until green".
---

# Hive E2E Test Harness

Reusable harness that exercises the full Hive stack against a running backend.

## Prerequisites
- Backend running on `http://localhost:8000` (or set `HIVE_BASE`).
  Start it with:
  ```
  cd /Users/rshetty/hive/backend && source venv/bin/activate && set -a && source /Users/rshetty/hive/.env; set +a && OPENCLAW_DEPLOY_MODE=local uvicorn main:app --port 8000
  ```
- A real `OPENROUTER_API_KEY` in `.env` (the harness uses it for hosted-agent invoke so the LLM call returns real output).

## Run
```
cd /Users/rshetty/hive/backend && source venv/bin/activate
python /Users/rshetty/hive/tests/e2e_harness.py                 # one run
python /Users/rshetty/hive/tests/e2e_harness.py --loop          # retry until all pass
HIVE_BASE=http://other:8000 python tests/e2e_harness.py          # custom base
```

## What it checks
1. Register + login user.
2. Skills list loads.
3. Hosted BYOK deploy via `POST /api/agents/deploy-hosted` (framework + model key + skills + MCP servers).
4. Agent is `active` and has an assigned `endpoint_url`.
5. Dashboard proxy `/a/{slug}/` returns the real agent app (auth via JWT).
6. `POST {endpoint_url}` invoke returns 200 with real output.
7. One-click OpenClaw deploy (`/api/agents/deploy-openclaw`) still works.
8. Settings provider key save (`/api/settings/keys`).
9. Delegation create + `task_result` stored (polls `/api/delegate/my-delegations`).
10. Frontend assets served: `/css/theme.css`, `/js/nav.js`, `/deploy`, `/login`, `/signup`.

## Notes
- Each run creates a fresh user + agents (no shared state collisions).
- The harness exits non-zero if any check fails — safe to use in a loop or CI gate.
- Hosted agent runtime is spawned locally via `services/openclaw_local.py` (no Docker needed).
