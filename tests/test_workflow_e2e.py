"""
Hive Workflow end-to-end test harness.

Exercises the full workflow lifecycle: create workflow, add steps, run,
stream progress, verify results.  Runs against a live backend.

    python tests/test_workflow_e2e.py
    python tests/test_workflow_e2e.py --base http://localhost:8000 --loop

Environment:
    HIVE_BASE  base URL of the API (default http://localhost:8000)
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _req(method: str, url: str, body=None, headers: dict | None = None,
         token: str = None, timeout: int = 60):
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def _json(status: int, raw: str):
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------

@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class WorkflowHarness:
    base: str
    checks: list[Check] = field(default_factory=list)
    token: str | None = None
    email: str | None = None
    user_id: str | None = None
    agent_id: str | None = None
    agent_api_key: str | None = None
    workflow_id: str | None = None
    step_ids: list[str] = field(default_factory=list)

    # ---- helpers ----
    def _call(self, method, path, body=None, token=None, timeout=60):
        return _req(method, self.base + path, body, token=token, timeout=timeout)

    def _ok(self, name: str, ok: bool, detail: str = ""):
        self.checks.append(Check(name, ok, detail))
        mark = "PASS" if ok else "FAIL"
        suffix = f" -- {detail}" if detail and not ok else ""
        print(f"  [{mark}] {name}{suffix}")

    def _post(self, path, body, token=None, **kw):
        s, c = self._call("POST", path, body, token=token, **kw)
        return s, _json(s, c), c

    def _get(self, path, token=None, **kw):
        s, c = self._call("GET", path, token=token, **kw)
        return s, _json(s, c), c

    def _put(self, path, body, token=None, **kw):
        s, c = self._call("PUT", path, body, token=token, **kw)
        return s, _json(s, c), c

    def _delete(self, path, token=None, **kw):
        s, c = self._call("DELETE", path, token=token, **kw)
        return s, _json(s, c), c

    def _grant_tokens(self, user_email: str, amount: float = 10000.0):
        here = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            os.path.join(here, "..", "backend", "agent_marketplace.db"),
            os.path.join(here, "..", "data", "agent_marketplace.db"),
            "/opt/hive/data/agent_marketplace.db",
            os.path.join(os.getcwd(), "agent_marketplace.db"),
            "agent_marketplace.db",
        ]
        db_path = next((p for p in candidates if os.path.exists(p)), None)
        if not db_path:
            print("    [warn] wallet DB not found; skipping token grant")
            return
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        row = cur.execute("SELECT id FROM users WHERE email=?", (user_email,)).fetchone()
        if not row:
            conn.close()
            return
        user_id = row[0]
        cur.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (amount, user_id))
        if cur.rowcount == 0:
            cur.execute("INSERT INTO wallets (user_id, balance) VALUES (?, ?)", (user_id, amount))
        conn.commit()
        conn.close()

    # ==================================================================
    # PHASE 1 — Auth + Fund wallet
    # ==================================================================
    def _phase_auth(self):
        print("\n--- Phase 1: Authentication ---")
        uname = f"wf_e2e_{uuid.uuid4().hex[:8]}"
        self.email = f"{uname}@example.com"
        password = "WfTest123!"

        s, d, _ = self._post("/api/auth/register", {
            "name": uname, "email": self.email, "password": password,
        })
        self._ok("register user", s == 200, f"status {s}")
        if s != 200:
            return False
        self.user_id = d.get("id")

        s, d, _ = self._post("/api/auth/login", {
            "email": self.email, "password": password,
        })
        self.token = d.get("access_token")
        self._ok("login user", s == 200 and bool(self.token), f"status {s}")

        # Fund wallet
        self._grant_tokens(self.email, 10000.0)

        # Verify wallet balance
        s, d, _ = self._get("/api/wallet/balance", token=self.token)
        self._ok("wallet funded", s == 200 and d.get("balance", 0) >= 5000,
                 f"balance={d.get('balance')}")
        return bool(self.token)

    # ==================================================================
    # PHASE 2 — Deploy an agent to use in workflows
    # ==================================================================
    def _phase_deploy_agent(self):
        print("\n--- Phase 2: Deploy Agent ---")
        api_key = os.getenv("OPENROUTER_API_KEY", "sk-or-v1-fake")

        s, res, raw = self._post("/api/agents/deploy-hosted", {
            "name": "WF Test Agent",
            "description": "Agent for workflow e2e tests",
            "framework": "openclaw",
            "model_key": {"openrouter": api_key},
            "skill_names": ["terminal", "web_extract"],
            "tags": ["e2e", "workflow"],
        }, token=self.token)
        self._ok("deploy agent", s == 200, f"status {s} {raw[:120]}")
        if s != 200:
            return False

        self.agent_id = res.get("agent_id")
        self.agent_api_key = res.get("api_key")
        self._ok("agent has id", bool(self.agent_id))
        self._ok("agent has api_key", bool(self.agent_api_key))
        self._ok("agent has endpoint", bool(res.get("endpoint_url")))
        self._ok("agent status active", res.get("status") == "active",
                 f"status={res.get('status')}")
        return bool(self.agent_id)

    # ==================================================================
    # PHASE 3 — Workflow CRUD
    # ==================================================================
    def _phase_workflow_crud(self):
        print("\n--- Phase 3: Workflow CRUD ---")

        # CREATE workflow (no steps yet)
        s, wf, raw = self._post("/api/workflows", {
            "name": "E2E Test Workflow",
            "description": "Created by workflow e2e harness",
            "max_tokens_per_run": 500,
            "timeout_seconds": 300,
        }, token=self.token)
        self._ok("create workflow", s == 201, f"status {s} {raw[:120]}")
        if s != 201:
            return False
        self.workflow_id = wf["id"]

        # LIST workflows
        s, wfs, _ = self._get("/api/workflows", token=self.token)
        ids = [w["id"] for w in wfs] if isinstance(wfs, list) else []
        self._ok("list workflows", s == 200 and self.workflow_id in ids,
                 f"found {len(wfs) if isinstance(wfs, list) else '?'} workflows")

        # GET workflow detail
        s, wf2, _ = self._get(f"/api/workflows/{self.workflow_id}", token=self.token)
        self._ok("get workflow", s == 200 and wf2.get("name") == "E2E Test Workflow",
                 f"name={wf2.get('name')}")

        # UPDATE workflow
        s, wf3, _ = self._put(f"/api/workflows/{self.workflow_id}", {
            "name": "E2E Test Workflow (updated)",
            "status": "active",
        }, token=self.token)
        self._ok("update workflow", s == 200 and wf3.get("name").endswith("updated"),
                 f"name={wf3.get('name')} status={wf3.get('status')}")
        return True

    # ==================================================================
    # PHASE 4 — Step management
    # ==================================================================
    def _phase_steps(self):
        print("\n--- Phase 4: Step Management ---")
        if not self.workflow_id or not self.agent_id:
            self._ok("steps", False, "no workflow or agent")
            return False

        # ADD step 1
        s, step1, raw = self._post(f"/api/workflows/{self.workflow_id}/steps", {
            "agent_id": self.agent_id,
            "name": "Step 1: Process Input",
            "description": "First step in the pipeline",
            "step_order": 0,
            "task_template": "Process this input: {{workflow_input.query}}",
            "max_tokens": 100,
            "timeout_seconds": 120,
        }, token=self.token)
        self._ok("add step 1", s == 201, f"status {s} {raw[:120]}")
        if s == 201:
            self.step_ids.append(step1["id"])

        # ADD step 2
        s, step2, raw = self._post(f"/api/workflows/{self.workflow_id}/steps", {
            "agent_id": self.agent_id,
            "name": "Step 2: Transform",
            "description": "Second step",
            "step_order": 1,
            "task_template": "Transform the result: {{prev_output}}",
            "max_tokens": 100,
            "timeout_seconds": 120,
        }, token=self.token)
        self._ok("add step 2", s == 201, f"status {s} {raw[:120]}")
        if s == 201:
            self.step_ids.append(step2["id"])

        # ADD step 3
        s, step3, raw = self._post(f"/api/workflows/{self.workflow_id}/steps", {
            "agent_id": self.agent_id,
            "name": "Step 3: Finalize",
            "step_order": 2,
            "task_template": "Summarize: {{prev_output}}",
            "max_tokens": 50,
            "timeout_seconds": 60,
        }, token=self.token)
        self._ok("add step 3", s == 201, f"status {s} {raw[:120]}")
        if s == 201:
            self.step_ids.append(step3["id"])

        # VERIFY step count in workflow
        s, wf, _ = self._get(f"/api/workflows/{self.workflow_id}", token=self.token)
        self._ok("workflow has 3 steps", s == 200 and len(wf.get("steps", [])) == 3,
                 f"steps={len(wf.get('steps', []))}")

        # UPDATE step
        if self.step_ids:
            s, updated, _ = self._put(
                f"/api/workflows/{self.workflow_id}/steps/{self.step_ids[0]}",
                {"task_template": "Updated template: {{workflow_input.query}}", "max_tokens": 150},
                token=self.token)
            self._ok("update step", s == 200 and updated.get("max_tokens") == 150,
                     f"max_tokens={updated.get('max_tokens')}")

        return True

    # ==================================================================
    # PHASE 5 — Workflow execution
    # ==================================================================
    def _phase_run(self):
        print("\n--- Phase 5: Workflow Execution ---")
        if not self.workflow_id:
            self._ok("run", False, "no workflow")
            return False

        # START run
        s, run, raw = self._post(f"/api/workflows/{self.workflow_id}/run", {
            "input_data": {"query": "What is 2+2? Reply with just the number."},
        }, token=self.token)
        self._ok("start workflow run", s == 200, f"status {s} {raw[:150]}")
        if s != 200:
            return False

        run_id = run.get("id")
        self._ok("run has id", bool(run_id), f"run_id={run_id}")

        # LIST runs
        s, runs, _ = self._get(f"/api/workflows/{self.workflow_id}/runs", token=self.token)
        run_ids = [r["id"] for r in runs] if isinstance(runs, list) else []
        self._ok("list runs", s == 200 and run_id in run_ids,
                 f"found {len(runs) if isinstance(runs, list) else '?'} runs")

        # Poll for completion (max 120s)
        print("    Polling for workflow completion...")
        final_status = None
        for i in range(60):
            s, run_detail, _ = self._get(
                f"/api/workflows/{self.workflow_id}/runs/{run_id}", token=self.token)
            if s == 200:
                status = run_detail.get("status")
                if status in ("completed", "failed"):
                    final_status = status
                    break
            time.sleep(2)

        self._ok("workflow completed", final_status == "completed",
                 f"final_status={final_status}")

        # GET run detail
        s, run_detail, _ = self._get(
            f"/api/workflows/{self.workflow_id}/runs/{run_id}", token=self.token)
        if s == 200:
            self._ok("run has output_data", bool(run_detail.get("output_data")),
                     f"output_keys={list(run_detail.get('output_data', {}).keys()) if run_detail.get('output_data') else 'None'}")
            self._ok("run has step_runs", len(run_detail.get("step_runs", [])) == 3,
                     f"step_runs={len(run_detail.get('step_runs', []))}")

            # Check each step run
            for sr in run_detail.get("step_runs", []):
                self._ok(f"  step '{sr.get('step_order')}' has status",
                         sr.get("status") in ("completed", "failed", "skipped"),
                         f"status={sr.get('status')}")

        return final_status == "completed"

    # ==================================================================
    # PHASE 6 — Error cases
    # ==================================================================
    def _phase_errors(self):
        print("\n--- Phase 6: Error Cases ---")

        # Run non-active workflow → 400
        # First, set workflow back to draft
        self._put(f"/api/workflows/{self.workflow_id}", {"status": "draft"}, token=self.token)
        s, _, raw = self._post(f"/api/workflows/{self.workflow_id}/run", {
            "input_data": {"query": "test"},
        }, token=self.token)
        self._ok("run draft workflow → 400", s == 400, f"status {s}")
        # Restore to active
        self._put(f"/api/workflows/{self.workflow_id}", {"status": "active"}, token=self.token)

        # Create workflow with empty name → 422
        s, _, _ = self._post("/api/workflows", {
            "name": "",
        }, token=self.token)
        self._ok("create workflow empty name → 422", s == 422, f"status {s}")

        # Get non-existent workflow → 404
        s, _, _ = self._get("/api/workflows/nonexistent-id", token=self.token)
        self._ok("get nonexistent workflow → 404", s == 404, f"status {s}")

        # Add step with non-existent agent → 404
        s, _, _ = self._post(f"/api/workflows/{self.workflow_id}/steps", {
            "agent_id": "nonexistent-agent-id",
            "name": "Bad Step",
            "task_template": "do something",
        }, token=self.token)
        self._ok("add step bad agent → 404", s == 404, f"status {s}")

        # Unauthorized access → 401
        s, _, _ = self._get("/api/workflows", token="bad-token")
        self._ok("unauthorized → 401/403", s in (401, 403), f"status {s}")

        return True

    # ==================================================================
    # PHASE 7 — Frontend pages served
    # ==================================================================
    def _phase_frontend(self):
        print("\n--- Phase 7: Frontend Pages ---")
        pages = ["/workflows", "/workflows/new"]
        for p in pages:
            s, _, _ = self._get(p)
            self._ok(f"page {p}", s == 200, f"status {s}")

        # Workflow builder page (by ID)
        if self.workflow_id:
            s, _, _ = self._get(f"/workflows/{self.workflow_id}")
            self._ok(f"page /workflows/{self.workflow_id[:8]}...", s == 200, f"status {s}")

        # Static assets
        assets = ["/css/theme.css", "/js/sidebar.js", "/js/app.js"]
        for a in assets:
            s, _, _ = self._get(a)
            self._ok(f"static {a}", s == 200, f"status {s}")

        return True

    # ==================================================================
    # PHASE 8 — Cleanup
    # ==================================================================
    def _phase_cleanup(self):
        print("\n--- Phase 8: Cleanup ---")
        if self.workflow_id:
            s, _, _ = self._delete(f"/api/workflows/{self.workflow_id}", token=self.token)
            self._ok("delete workflow", s in (200, 204), f"status {s}")

        if self.agent_id:
            s, _, _ = self._delete(f"/api/agents/{self.agent_id}", token=self.token)
            self._ok("delete agent", s in (200, 204, 404), f"status {s}")
        return True

    # ==================================================================
    # MAIN
    # ==================================================================
    def run(self) -> bool:
        phases = [
            ("auth",         self._phase_auth),
            ("deploy_agent", self._phase_deploy_agent),
            ("workflow_crud", self._phase_workflow_crud),
            ("steps",        self._phase_steps),
            ("run",          self._phase_run),
            ("errors",       self._phase_errors),
            ("frontend",     self._phase_frontend),
            ("cleanup",      self._phase_cleanup),
        ]
        for name, fn in phases:
            try:
                ok = fn()
            except Exception as exc:  # noqa: BLE001
                self._ok(f"phase {name}", False, f"exception: {exc}")
                ok = False
            if not ok and name in ("auth",):
                print(f"\nFATAL: phase {name} failed — aborting")
                break

        failed = [c for c in self.checks if not c.ok]
        total = len(self.checks)
        passed = total - len(failed)
        print(f"\n{'='*60}")
        print(f"  {passed}/{total} checks passed")
        if failed:
            print(f"\n  FAILURES:")
            for c in failed:
                print(f"    - {c.name}: {c.detail}")
        else:
            print("  ALL GREEN")
        print(f"{'='*60}")
        return len(failed) == 0


def main():
    ap = argparse.ArgumentParser(description="Hive Workflow E2E Harness")
    ap.add_argument("--base", default=os.getenv("HIVE_BASE", "http://localhost:8000"))
    ap.add_argument("--loop", action="store_true", help="Retry until all pass")
    args = ap.parse_args()

    attempt = 0
    while True:
        attempt += 1
        print(f"\n{'#'*60}")
        print(f"  Workflow E2E Run #{attempt}  base={args.base}")
        print(f"{'#'*60}")
        h = WorkflowHarness(base=args.base)
        ok = h.run()
        if ok or not args.loop:
            return 0 if ok else 1
        print(f"\nRetrying in 5s...")
        time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
