"""
Hive end-to-end test harness.

Runs a battery of checks against a running Hive backend (+ frontend assets)
and exits non-zero if any check fails. Designed to be looped:

    python tests/e2e_harness.py            # uses defaults
    python tests/e2e_harness.py --base http://localhost:8000 --loop

Environment:
    HIVE_BASE   base URL of the API (default http://localhost:8000)
    HIVE_EMAIL  login email to reuse (optional; a fresh user is created each run)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field


@dataclass
class Result:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Harness:
    base: str
    results: list = field(default_factory=list)
    token: str | None = None
    agent_id: str | None = None
    slug: str | None = None

    # ---- low-level http -------------------------------------------------
    def call(self, method: str, path: str, body=None, token=None, timeout=60):
        url = self.base + path
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
            return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()
        except Exception as e:  # noqa: BLE001
            return 0, str(e)

    # ---- assertions -----------------------------------------------------
    def check(self, name: str, ok: bool, detail: str = ""):
        self.results.append(Result(name, ok, detail))
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}" + (f" — {detail}" if detail and not ok else ""))

    # ---- scenario -------------------------------------------------------
    def run(self) -> bool:
        uname = f"e2e_{uuid.uuid4().hex[:8]}"
        email = f"{uname}@example.com"
        password = "Test1234!"

        # 1. Auth: register + login
        s, c = self.call("POST", "/api/auth/register",
                         {"name": uname, "email": email, "password": password})
        self.check("register user", s == 200, f"status {s}")
        if s != 200:
            return False
        s, c = self.call("POST", "/api/auth/login", {"email": email, "password": password})
        if s != 200:
            self.check("login user", False, f"status {s} {c[:120]}")
            return False
        self.token = json.loads(c).get("access_token")
        self.check("login user", bool(self.token))

        # 2. Skills list loads
        s, c = self.call("GET", "/api/skills", token=self.token)
        skills = json.loads(c) if s == 200 else []
        self.check("skills list", s == 200 and len(skills) > 0, f"{len(skills)} skills")

        # 3. Hosted BYOK deploy
        skill_names = [sk["name"] for sk in skills[:3]]
        s, c = self.call("POST", "/api/agents/deploy-hosted", {
            "name": "E2E Hosted Agent",
            "description": "harness",
            "framework": "openclaw",
            "model_key": {"provider": "openrouter", "key": os.getenv("OPENROUTER_API_KEY", "sk-or-v1-fake")},
            "skill_names": skill_names,
            "mcp_servers": [{"name": "demo", "url": "https://mcp.example.com"}],
            "tags": ["e2e"],
        }, token=self.token)
        res = json.loads(c) if s == 200 else {}
        self.agent_id = res.get("agent_id")
        self.slug = res.get("slug")
        self.agent_api_key = res.get("api_key")
        self.check("deploy-hosted", s == 200 and self.agent_id,
                   f"status {s} {c[:120]}")
        if s != 200:
            return False

        # 4. Agent is active + endpoint assigned
        self.check("endpoint assigned",
                   bool(res.get("endpoint_url")) and res.get("status") == "active",
                   res.get("status", ""))

        # 5. Wait for the agent runtime to come up, then check dashboard + invoke
        self._wait_for_agent(res["endpoint_url"], token=self.token)

        # 6. Dashboard proxy loads real agent app (cookie auth)
        self._check_dashboard()

        # 7. Invoke the hosted agent
        s, c = self.call("POST", res["endpoint_url"],
                         {"task": "reply with the single word: pong"}, token=self.token,
                         timeout=90)
        self.check("invoke agent", s == 200, f"status {s} {c[:120]}")

        # 7. Swarm / openclaw deploy still works
        s, c = self.call("POST", "/api/agents/deploy-openclaw", {
            "agent_name": "E2E OpenClaw", "extra_skill_names": [], "tags": ["e2e"]
        }, token=self.token)
        self.check("deploy-openclaw", s == 200, f"status {s} {c[:120]}")
        oc_res = json.loads(c) if s == 200 else {}
        oc_id = oc_res.get("agent_id")

        # 8. Save a settings provider key
        s, c = self.call("PATCH", "/api/me/keys", {
            "openrouter": "sk-or-v1-harness"
        }, token=self.token)
        self.check("settings save key", s in (200, 201, 204), f"status {s}")

        # 9. Delegate a task (own agent) and verify task_result stored
        # Fund the user's wallet so the delegation escrow can succeed.
        self._grant_tokens(email)
        self._check_delegation()

        # 10. Frontend assets are served
        for path in ("/css/theme.css", "/js/nav.js", "/deploy", "/login", "/signup"):
            s, _ = self.call("GET", path)
            self.check(f"static {path}", s == 200, f"status {s}")

        # 11. Cleanup: stop runtimes + delete test agents so they don't pile up.
        if self.agent_id:
            self.call("DELETE", f"/api/agents/{self.agent_id}", token=self.token)
        if oc_id:
            self.call("DELETE", f"/api/agents/{oc_id}", token=self.token)

        return all(r.ok for r in self.results)

    def _agent_status(self):
        if not self.agent_id:
            return "unknown"
        s, c = self.call("GET", f"/api/agents/{self.agent_id}", token=self.token)
        if s == 200:
            return json.loads(c).get("status", "?")
        return f"http{s}"

    def _grant_tokens(self, user_email: str, amount: float = 5000.0):
        """Fund the test user's wallet directly (local e2e harness only)."""
        import sqlite3
        db_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "backend", "agent_marketplace.db",
        )
        if not os.path.exists(db_path):
            db_path = "agent_marketplace.db"
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        row = cur.execute(
            "SELECT id FROM users WHERE email=?", (user_email,)
        ).fetchone()
        if not row:
            conn.close()
            return
        user_id = row[0]
        cur.execute(
            "UPDATE wallets SET balance = balance + ? WHERE user_id=?",
            (amount, user_id),
        )
        if cur.rowcount == 0:
            cur.execute(
                "INSERT INTO wallets (user_id, balance) VALUES (?, ?)",
                (user_id, amount),
            )
        conn.commit()
        conn.close()

    def _wait_for_agent(self, endpoint_url, token=None, timeout=40):
        """Poll the agent's health until it responds (runtime may still be booting)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            s, c = self.call("GET", endpoint_url.replace("/invoke", "/health") + "?token=x",
                             token=token)
            if s == 200:
                return True
            time.sleep(2)
        return False

    def _check_dashboard(self):
        if not self.slug:
            self.check("dashboard proxy", False, "no slug")
            return
        s, c = self.call("GET", f"/a/{self.slug}/", token=self.token)
        # The agent app is a large HTML doc (>1KB). A login page is ~2KB but
        # lacks the agent-name marker and the alpine dashboard script.
        is_agent_app = (
            s == 200
            and len(c) > 1000
            and ("/status" in c or "chatInput" in c or "Alpine" in c or "agent" in c.lower())
            and "Sign in to Hive" not in c
        )
        self.check("dashboard proxy", is_agent_app, f"status {s} len {len(c)}")

    def call_key(self, method, path, body=None, api_key=None, timeout=120):
        url = self.base + path
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["X-API-Key"] = api_key
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
            return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()
        except Exception as e:  # noqa: BLE001
            return 0, str(e)

    def _check_delegation(self):
        if not self.agent_id or not self.agent_api_key:
            self.check("delegation task_result", False, "no agent/key")
            return
        # Agent-to-agent delegation: caller agent authenticates with X-API-Key.
        s, c = self.call_key("POST", "/api/delegate/request", {
            "task_description": "say hello in one word",
            "target_agent_id": self.agent_id,
            "max_tokens": 1000,
        }, api_key=self.agent_api_key, timeout=120)
        if s != 200:
            self.check("delegation create", False, f"status {s} {c[:120]}")
            return
        self.check("delegation create", True)
        data = json.loads(c)
        delegation_id = data.get("delegation_id") or data.get("id")
        if not delegation_id:
            self.check("delegation result", False, "no delegation id")
            return
        # poll for task_result (user-initiated delegations view)
        for _ in range(40):
            s, c = self.call("GET", "/api/delegate/user-delegations", token=self.token)
            if s == 200:
                items = json.loads(c).get("delegations", [])
                for d in items:
                    if d.get("id") == delegation_id:
                        if d.get("task_result"):
                            self.check("delegation task_result", True)
                            return
            time.sleep(3)
        self.check("delegation task_result", False, "no result within timeout")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.getenv("HIVE_BASE", "http://localhost:8000"))
    ap.add_argument("--loop", action="store_true", help="retry until all pass")
    args = ap.parse_args()

    attempt = 0
    while True:
        attempt += 1
        print(f"\n=== Hive e2e run #{attempt} ({args.base}) ===")
        h = Harness(base=args.base)
        ok = h.run()
        failed = [r for r in h.results if not r.ok]
        print(f"\n{len(h.results)-len(failed)}/{len(h.results)} checks passed.")
        if ok:
            print("ALL GREEN ✅")
            return 0
        if not args.loop:
            print(f"{len(failed)} FAILED ❌")
            for r in failed:
                print(f"   - {r.name}: {r.detail}")
            return 1
        print(f"{len(failed)} failed — retrying in 5s…")
        time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
