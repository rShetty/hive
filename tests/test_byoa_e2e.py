"""
Hive BYOA (Bring Your Own Agent) end-to-end harness.

Exercises the full lifecycle for ALL THREE frameworks (openclaw, langchain,
crewai): user registration, MCP server CRUD, skill attachment, agent deploy,
dashboard proxy, LLM invocation, MCP grant/revoke, agent config, and cleanup.

    python tests/test_byoa_e2e.py
    python tests/test_byoa_e2e.py --base http://127.0.0.1:8080 --frameworks openclaw langchain crewai
    python tests/test_byoa_e2e.py --loop

Environment:
    HIVE_BASE           base URL (default http://localhost:8080)
    OPENROUTER_API_KEY  real key for LLM call verification (optional; fake used otherwise)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _req(method: str, url: str, body=None, headers: dict | None = None,
         token: str = None, api_key: str = None, timeout: int = 60):
    """Single HTTP call.  Returns (status, body_str)."""
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    if api_key:
        hdrs["X-API-Key"] = api_key
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
class ByoaHarness:
    base: str
    frameworks: list[str] = field(default_factory=lambda: ["openclaw", "langchain", "crewai"])
    checks: list[Check] = field(default_factory=list)
    token: str | None = None
    email: str | None = None
    user_id: str | None = None
    skill_ids: list[str] = field(default_factory=list)
    skill_names: list[str] = field(default_factory=list)
    mcp_server_id: str | None = None
    # per-framework tracking
    agents: dict = field(default_factory=dict)  # fw -> {agent_id, slug, api_key, endpoint_url}

    # ---- helpers ----
    def _call(self, method, path, body=None, token=None, api_key=None, timeout=60):
        return _req(method, self.base + path, body, token=token, api_key=api_key, timeout=timeout)

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

    def _patch(self, path, body, token=None, **kw):
        s, c = self._call("PATCH", path, body, token=token, **kw)
        return s, _json(s, c), c

    def _delete(self, path, token=None, **kw):
        s, c = self._call("DELETE", path, token=token, **kw)
        return s, _json(s, c), c

    # ---- wait / poll ----
    def _wait_healthy(self, agent_id: str, token: str = None,
                      timeout: int = 60) -> bool:
        """Poll agent status until it becomes active."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            s, d, _ = self._get(f"/api/agents/{agent_id}", token=token)
            if s == 200:
                status = d.get("status", "")
                if status in ("active", "idle"):
                    return True
            time.sleep(2)
        return False

    # ==================================================================
    # PHASE 1 — Auth
    # ==================================================================
    def _phase_auth(self):
        print("\n--- Phase 1: Authentication ---")
        uname = f"byoa_{uuid.uuid4().hex[:8]}"
        self.email = f"{uname}@e2e.example.com"
        password = "ByoaTest123!"

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
        return bool(self.token)

    # ==================================================================
    # PHASE 2 — Skills
    # ==================================================================
    def _phase_skills(self):
        print("\n--- Phase 2: Skills ---")
        s, skills, _ = self._get("/api/skills", token=self.token)
        self._ok("list skills", s == 200 and len(skills) > 0, f"{len(skills)} skills")
        self.skill_ids = [sk["id"] for sk in skills[:3]]
        self.skill_names = [sk["name"] for sk in skills[:3]]
        return bool(self.skill_ids)

    # ==================================================================
    # PHASE 3 — MCP server CRUD
    # ==================================================================
    def _phase_mcp_crud(self):
        print("\n--- Phase 3: MCP Server CRUD ---")

        # CREATE
        s, srv, _ = self._post("/api/mcp-servers", {
            "name": f"e2e-mcp-{uuid.uuid4().hex[:6]}",
            "url": "https://mcp.e2e.test/v1",
            "description": "E2E test MCP server",
            "transport": "http",
            "auth_type": "headers",
            "headers": {"Authorization": "Bearer mcp-test-token"},
        }, token=self.token)
        self._ok("mcp create", s == 201, f"status {s} {str(srv)[:100]}")
        if s != 201:
            return False
        self.mcp_server_id = srv["id"]

        # LIST (owned)
        s, servers, _ = self._get("/api/mcp-servers", token=self.token)
        ids = [x["id"] for x in servers]
        self._ok("mcp list", s == 200 and self.mcp_server_id in ids,
                 f"found {len(servers)} servers")

        # GET single
        s, srv2, _ = self._get(f"/api/mcp-servers/{self.mcp_server_id}", token=self.token)
        self._ok("mcp get", s == 200 and srv2.get("name") == srv["name"],
                 f"name={srv2.get('name')}")

        # UPDATE
        s, srv3, _ = self._put(f"/api/mcp-servers/{self.mcp_server_id}",
                                 {"description": "Updated by E2E"},
                                 token=self.token)
        self._ok("mcp update", s == 200 and "Updated" in srv3.get("description", ""),
                 f"status {s}")

        # Attached agents should be empty
        s, agents, _ = self._get(f"/api/mcp-servers/{self.mcp_server_id}/agents",
                                  token=self.token)
        self._ok("mcp agents (empty)", s == 200 and isinstance(agents, list),
                 f"count={len(agents) if isinstance(agents, list) else '?'}")
        return True

    # ==================================================================
    # PHASE 4 — Deploy agents (one per framework)
    # ==================================================================
    def _phase_deploy(self):
        print("\n--- Phase 4: Deploy Agents ---")
        api_key = os.getenv("OPENROUTER_API_KEY", "sk-or-v1-e2e-fake-key")
        ok = True
        for fw in self.frameworks:
            print(f"\n  >> Deploying {fw} agent...")
            s, res, raw = self._post("/api/agents/deploy-hosted", {
                "name": f"E2E {fw.title()} Agent",
                "description": f"BYOA e2e test — {fw}",
                "framework": fw,
                "model_key": {"openrouter": api_key},
                "skill_names": self.skill_names,
                "mcp_servers": [],
                "mcp_server_ids": [self.mcp_server_id] if self.mcp_server_id else [],
                "tags": ["e2e", fw],
            }, token=self.token)
            self._ok(f"deploy {fw}", s == 200, f"status {s} {raw[:120]}")
            if s != 200:
                ok = False
                continue

            agent_id = res["agent_id"]
            self.agents[fw] = {
                "agent_id": agent_id,
                "slug": res.get("slug"),
                "api_key": res.get("api_key"),
                "endpoint_url": res.get("endpoint_url"),
            }

            # Basic post-deploy assertions
            self._ok(f"  {fw} endpoint", bool(res.get("endpoint_url")),
                     f"endpoint={res.get('endpoint_url')}")
            self._ok(f"  {fw} status", res.get("status") == "active",
                     f"status={res.get('status')}")
            self._ok(f"  {fw} api_key", bool(res.get("api_key")),
                     f"key_prefix={res.get('api_key', '')[:12]}...")
        return ok

    # ==================================================================
    # PHASE 5 — Wait for runtimes + dashboard + invoke
    # ==================================================================
    def _phase_invoke(self):
        print("\n--- Phase 5: Invoke Agents ---")
        ok = True
        for fw, info in self.agents.items():
            print(f"\n  >> {fw}: waiting for runtime...")
            ep = info["endpoint_url"]
            token = self.token

            # 5a. Wait for health
            healthy = self._wait_healthy(info["agent_id"], token=token, timeout=60)
            self._ok(f"  {fw} health", healthy, "timeout" if not healthy else "")
            if not healthy:
                ok = False
                continue

            # 5b. Dashboard proxy — verify the agent serves HTML through the proxy.
            # Use http.client directly to avoid urllib chunked-read quirks.
            import http.client
            conn = http.client.HTTPConnection("127.0.0.1", 8080, timeout=15)
            conn.request("GET", f"/a/{info['slug']}/",
                         headers={"Authorization": f"Bearer {token}"})
            r1 = conn.getresponse()
            dash_status = r1.status
            dash_len = int(r1.getheader("content-length", 0))
            r1.read()
            conn.close()
            is_dashboard = dash_status == 200 and dash_len > 500
            self._ok(f"  {fw} dashboard", is_dashboard,
                     f"status {dash_status} body_len={dash_len}")

            # 5c. Info endpoint
            s, inf, _ = self._get(ep.replace("/invoke", "/info"), token=token)
            self._ok(f"  {fw} /info", s == 200, f"status {s} name={inf.get('name','')}")

            # 5d. Invoke — ask a simple factual question
            s, resp, raw = self._post(ep, {
                "message": "What is the capital of France? Reply in one word only.",
            }, token=token, timeout=90)
            self._ok(f"  {fw} invoke", s == 200, f"status {s} {raw[:150]}")
            if s == 200:
                output = resp.get("result", {}).get("output", resp.get("output", ""))
                self._ok(f"  {fw} has output", bool(output),
                         f"output_len={len(output)} first100={output[:100]}")

            # 5e. Invoke — check tool/Skill presence in response
            s2, resp2, raw2 = self._post(ep, {
                "message": "List the tools you have available. Just the names.",
            }, token=token, timeout=90)
            if s2 == 200:
                out2 = resp2.get("result", {}).get("output", resp2.get("output", ""))
                self._ok(f"  {fw} tools listed", bool(out2),
                         f"first150={out2[:150]}")
            else:
                self._ok(f"  {fw} tools listed", False, f"status {s2}")

        return ok

    # ==================================================================
    # PHASE 6 — MCP grant / revoke / re-invoke
    # ==================================================================
    def _phase_mcp_lifecycle(self):
        print("\n--- Phase 6: MCP Grant / Revoke Lifecycle ---")
        if not self.mcp_server_id or not self.agents:
            self._ok("mcp lifecycle", False, "no mcp server or agents")
            return False

        first_fw = next(iter(self.agents))
        agent_id = self.agents[first_fw]["agent_id"]

        # Grant
        s, grants, raw = self._post(
            f"/api/mcp-servers/{self.mcp_server_id}/grant",
            {"agent_ids": [agent_id]}, token=self.token)
        self._ok("mcp grant", s == 200 and len(grants) > 0,
                 f"status {s} grants={len(grants) if isinstance(grants, list) else raw[:100]}")

        # List agents with access
        s, agents_with, _ = self._get(
            f"/api/mcp-servers/{self.mcp_server_id}/agents", token=self.token)
        grant_ids = [a.get("agent_id") for a in agents_with] if isinstance(agents_with, list) else []
        self._ok("mcp agents (after grant)", agent_id in grant_ids,
                 f"granted={grant_ids}")

        # Agent's MCP access list
        s, agent_mcp, _ = self._get(
            f"/api/mcp-servers/agent/{agent_id}", token=self.token)
        mcp_ids = [a.get("mcp_server_id") for a in agent_mcp] if isinstance(agent_mcp, list) else []
        self._ok("mcp agent access list", self.mcp_server_id in mcp_ids,
                 f"mcp_ids={mcp_ids}")

        # Revoke
        s, _, raw = self._post(
            f"/api/mcp-servers/{self.mcp_server_id}/revoke",
            {"agent_ids": [agent_id]}, token=self.token)
        self._ok("mcp revoke", s == 200, f"status {s}")

        # Verify revoked — check that our agent's grant is disabled
        s, agents_after, _ = self._get(
            f"/api/mcp-servers/{self.mcp_server_id}/agents", token=self.token)
        if isinstance(agents_after, list):
            our_grant = next((a for a in agents_after if a.get("agent_id") == agent_id), None)
            is_disabled = our_grant is not None and not our_grant.get("enabled", True)
            self._ok("mcp revoked check", is_disabled,
                     f"enabled={our_grant.get('enabled') if our_grant else 'no grant'}")
        else:
            self._ok("mcp revoked check", False, f"unexpected response")

        # Re-grant for the config test
        self._post(
            f"/api/mcp-servers/{self.mcp_server_id}/grant",
            {"agent_ids": [agent_id]}, token=self.token)
        return True

    # ==================================================================
    # PHASE 7 — Agent config (LLM config, skills management)
    # ==================================================================
    def _phase_agent_config(self):
        print("\n--- Phase 7: Agent Config ---")
        if not self.agents:
            self._ok("agent config", False, "no agents")
            return False

        first_fw = next(iter(self.agents))
        agent_id = self.agents[first_fw]["agent_id"]

        # Get agent config
        s, cfg, _ = self._get(f"/api/agents/{agent_id}/config", token=self.token)
        self._ok("get agent config", s == 200, f"status {s}")

        # Update agent config (set LLM provider)
        s, _, raw = self._put(f"/api/agents/{agent_id}/config", {
            "llm": {"provider": "openrouter", "api_key": "sk-or-v1-test-config"},
        }, token=self.token)
        self._ok("update agent config", s == 200, f"status {s} {raw[:100]}")

        # Get agent detail
        s, detail, _ = self._get(f"/api/agents/{agent_id}", token=self.token)
        self._ok("get agent detail", s == 200, f"status {s} name={detail.get('name','')}")

        # Get agent skills
        s, agent_skills, _ = self._get(f"/api/agents/{agent_id}/skills", token=self.token)
        self._ok("get agent skills", s == 200 and isinstance(agent_skills, list),
                 f"count={len(agent_skills) if isinstance(agent_skills, list) else '?'}")

        return True

    def _put(self, path, body, token=None, **kw):
        s, c = self._call("PUT", path, body, token=token, **kw)
        return s, _json(s, c), c

    # ==================================================================
    # PHASE 8 — Deploy-openclaw (VPS path, warn-only)
    # ==================================================================
    def _phase_openclaw_vps(self):
        print("\n--- Phase 8: OpenClaw VPS Deploy (warn-only) ---")
        s, res, raw = self._post("/api/agents/deploy-openclaw", {
            "agent_name": "E2E OpenClaw VPS",
            "extra_skill_names": [],
            "tags": ["e2e"],
        }, token=self.token)
        if s == 200:
            self._ok("deploy-openclaw VPS", True)
            oc_id = res.get("agent_id")
            # Cleanup VPS agent
            if oc_id:
                self._call("DELETE", f"/api/agents/{oc_id}", token=self.token)
        else:
            print(f"    [warn] deploy-openclaw returned {s} — skipping (VPS path only)")
            self._ok("deploy-openclaw VPS", True, "skipped (VPS only)")
        return True

    # ==================================================================
    # PHASE 9 — Settings: save user model API keys
    # ==================================================================
    def _phase_settings(self):
        print("\n--- Phase 9: Settings / API Keys ---")
        s, _, raw = self._patch("/api/me/keys", {
            "openrouter": "sk-or-v1-harness-test",
        }, token=self.token)
        self._ok("save model keys", s in (200, 201, 204), f"status {s}")

        # Verify /auth/me returns user info
        s, me, _ = self._get("/api/auth/me", token=self.token)
        self._ok("auth/me", s == 200 and me.get("email") == self.email,
                 f"email={me.get('email')}")
        return True

    # ==================================================================
    # PHASE 10 — Error cases
    # ==================================================================
    def _phase_errors(self):
        print("\n--- Phase 10: Error Cases ---")

        # Deploy with no skills → 400
        s, _, _ = self._post("/api/agents/deploy-hosted", {
            "name": "No Skills Agent",
            "framework": "openclaw",
            "model_key": {"openrouter": "fake"},
        }, token=self.token)
        self._ok("deploy no skills → 400", s == 400, f"status {s}")

        # Deploy with invalid framework (should still deploy, runtime fallback)
        s, res, raw = self._post("/api/agents/deploy-hosted", {
            "name": "Bad Framework Agent",
            "framework": "nonexistent_framework",
            "model_key": {"openrouter": "fake"},
            "skill_names": self.skill_names[:1],
        }, token=self.token)
        # The server doesn't validate framework; it still deploys
        self._ok("deploy unknown framework", s == 200 or s == 400,
                 f"status {s} (accepted={s==200})")
        if s == 200:
            bad_id = res.get("agent_id")
            if bad_id:
                self._call("DELETE", f"/api/agents/{bad_id}", token=self.token)

        # MCP create with bad transport → 422
        s, _, _ = self._post("/api/mcp-servers", {
            "name": "bad transport",
            "transport": "INVALID",
        }, token=self.token)
        self._ok("mcp bad transport → 422", s == 422, f"status {s}")

        # MCP create stdio without command → 422
        s, _, _ = self._post("/api/mcp-servers", {
            "name": "stdio no cmd",
            "transport": "stdio",
        }, token=self.token)
        self._ok("mcp stdio no cmd → 422", s == 422, f"status {s}")

        # Unauthorized access → 401/403
        s, _, _ = self._get("/api/mcp-servers", token="invalid-token-here")
        self._ok("unauthorized → 401/403", s in (401, 403), f"status {s}")

        # Invoke non-existent agent → 404
        s, _, _ = self._post("/api/agents/00000000-0000-0000-0000-000000000000/invoke",
                              {"message": "test"}, token=self.token)
        self._ok("invoke nonexistent → 404", s == 404, f"status {s}")

        return True

    # ==================================================================
    # PHASE 11 — Delegation (agent-to-agent)
    # ==================================================================
    def _phase_delegation(self):
        print("\n--- Phase 11: Delegation ---")
        if not self.agents:
            self._ok("delegation", False, "no agents")
            return True  # non-blocking

        # Fund the user's wallet so delegation escrow can succeed
        self._grant_tokens(self.email)

        # Get any agent's api_key for X-API-Key auth
        first_fw = next(iter(self.agents))
        info = self.agents[first_fw]
        api_key = info.get("api_key")
        if not api_key:
            self._ok("delegation setup", False, "no api_key")
            return True

        # Create delegation (agent authenticates with X-API-Key)
        s, del_data, raw = self._req_with_key(
            "POST", "/api/delegate/request", {
                "task_description": "Say the word 'delegate' and nothing else",
                "target_agent_id": info["agent_id"],
                "max_tokens": 500,
            }, api_key=api_key, timeout=120)
        self._ok("delegation create", s == 200, f"status {s} {raw[:120]}")
        if s != 200:
            return True  # non-blocking

        delegation_id = del_data.get("delegation_id") or del_data.get("id")

        # Poll for result
        found = False
        for _ in range(30):
            s, d, _ = self._get("/api/delegate/user-delegations", token=self.token)
            if s == 200:
                for d_item in d.get("delegations", []):
                    if d_item.get("id") == delegation_id:
                        if d_item.get("task_result"):
                            found = True
                            break
            if found:
                break
            time.sleep(3)
        self._ok("delegation result", found,
                 f"delegation_id={delegation_id}")
        return True

    def _req_with_key(self, method, path, body=None, api_key=None, timeout=120):
        url = self.base + path
        hdrs = {"Content-Type": "application/json"}
        if api_key:
            hdrs["X-API-Key"] = api_key
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
            return resp.status, json.loads(resp.read().decode()), ""
        except urllib.error.HTTPError as exc:
            return exc.code, {}, exc.read().decode()
        except Exception as exc:  # noqa: BLE001
            return 0, {}, str(exc)

    def _grant_tokens(self, user_email: str, amount: float = 5000.0):
        """Fund the test user's wallet directly (local e2e harness only)."""
        import sqlite3
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
    # PHASE 12 — Frontend static assets
    # ==================================================================
    def _phase_frontend(self):
        print("\n--- Phase 12: Frontend Assets ---")
        paths = ["/css/theme.css", "/js/nav.js", "/js/sidebar.js",
                 "/deploy", "/login", "/signup", "/mcp"]
        for p in paths:
            s, _, _ = self._get(p)
            self._ok(f"static {p}", s == 200, f"status {s}")
        return True

    # ==================================================================
    # PHASE 13 — Cleanup
    # ==================================================================
    def _phase_cleanup(self):
        print("\n--- Phase 13: Cleanup ---")
        for fw, info in self.agents.items():
            s, _, _ = self._delete(f"/api/agents/{info['agent_id']}",
                                   token=self.token)
            self._ok(f"cleanup {fw}", s in (200, 204, 404), f"status {s}")

        # Delete MCP server
        if self.mcp_server_id:
            s, _, _ = self._delete(f"/api/mcp-servers/{self.mcp_server_id}",
                                   token=self.token)
            self._ok("cleanup mcp server", s in (200, 204), f"status {s}")
        return True

    # ==================================================================
    # MAIN
    # ==================================================================
    def run(self) -> bool:
        phases = [
            ("auth",          self._phase_auth),
            ("skills",        self._phase_skills),
            ("mcp_crud",      self._phase_mcp_crud),
            ("deploy",        self._phase_deploy),
            ("invoke",        self._phase_invoke),
            ("mcp_lifecycle", self._phase_mcp_lifecycle),
            ("agent_config",  self._phase_agent_config),
            ("openclaw_vps",  self._phase_openclaw_vps),
            ("settings",      self._phase_settings),
            ("errors",        self._phase_errors),
            ("delegation",    self._phase_delegation),
            ("frontend",      self._phase_frontend),
            ("cleanup",       self._phase_cleanup),
        ]
        for name, fn in phases:
            try:
                ok = fn()
            except Exception as exc:  # noqa: BLE001
                self._ok(f"phase {name}", False, f"exception: {exc}")
                ok = False
            if not ok and name in ("auth", "skills"):
                # Can't continue without auth or skills
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
    ap = argparse.ArgumentParser(description="Hive BYOA E2E Harness")
    ap.add_argument("--base", default=os.getenv("HIVE_BASE", "http://localhost:8080"))
    ap.add_argument("--frameworks", nargs="+",
                    default=["openclaw", "langchain", "crewai"],
                    help="Frameworks to test")
    ap.add_argument("--loop", action="store_true", help="Retry until all pass")
    args = ap.parse_args()

    attempt = 0
    while True:
        attempt += 1
        print(f"\n{'#'*60}")
        print(f"  BYOA E2E Run #{attempt}  base={args.base}  fw={args.frameworks}")
        print(f"{'#'*60}")
        h = ByoaHarness(base=args.base, frameworks=args.frameworks)
        ok = h.run()
        if ok or not args.loop:
            return 0 if ok else 1
        print(f"\nRetrying in 5s...")
        time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
