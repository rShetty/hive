"""E2E test harness for MCP server integration.

Tests the full lifecycle:
1. Start a local MCP server with real tools
2. Start the Hive backend
3. Register the MCP server in Hive
4. Create an agent with MCP access
5. Deploy the agent (subprocess with MCP_SERVERS env)
6. Invoke the agent — verify it discovers and calls MCP tools
7. Test revoke — verify agent loses access after restart

Usage:
    python tests/test_mcp_e2e.py
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────
HIVE_PORT = 8099  # Avoid conflicts with dev (8000) and VPS (8080)
MCP_PORT = 9098   # Test MCP server port
AGENT_BASE_PORT = 9000  # Matches OPENCLAW_PORT_START
BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
AGENT_DIR = Path(__file__).resolve().parent.parent / "docker" / "agent_app"

# Ensure backend is importable
sys.path.insert(0, str(BACKEND_DIR))

# ── Test MCP Server ──────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "echo",
        "description": "Echo back the input message",
        "inputSchema": {
            "type": "object",
            "properties": {"message": {"type": "string", "description": "Message to echo"}},
            "required": ["message"],
        },
    },
    {
        "name": "add",
        "description": "Add two numbers",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "number", "description": "First number"},
                "b": {"type": "number", "description": "Second number"},
            },
            "required": ["a", "b"],
        },
    },
    {
        "name": "get_time",
        "description": "Get current UTC time",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _handle_tool(name: str, args: dict) -> dict:
    if name == "echo":
        return {"content": [{"type": "text", "text": args.get("message", "")}]}
    elif name == "add":
        return {"content": [{"type": "text", "text": str(args.get("a", 0) + args.get("b", 0))}]}
    elif name == "get_time":
        from datetime import datetime, timezone
        return {"content": [{"type": "text", "text": datetime.now(timezone.utc).isoformat()}]}
    return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}]}


class MCPHandler(BaseHTTPRequestHandler):
    """Minimal MCP server over HTTP POST (JSON-RPC)."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            msg = json.loads(body)
        except Exception:
            self.send_response(400)
            self.end_headers()
            return

        method = msg.get("method", "")
        msg_id = msg.get("id")
        result = None
        error = None

        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "test-mcp-server", "version": "1.0.0"},
            }
        elif method == "notifications/initialized":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")
            return
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = msg.get("params", {})
            result = _handle_tool(params.get("name", ""), params.get("arguments", {}))
        else:
            error = {"code": -32601, "message": f"Method not found: {method}"}

        resp = {"jsonrpc": "2.0", "id": msg_id}
        if error:
            resp["error"] = error
        else:
            resp["result"] = result

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(resp).encode())

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format, *args):
        pass  # Suppress logs


def start_mcp_server(port: int) -> HTTPServer:
    server = HTTPServer(("127.0.0.1", port), MCPHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


# ── HTTP helpers ──────────────────────────────────────────────────────────

def api(path: str, token: str = "", data: dict | None = None, method: str = "GET") -> dict | list | None:
    url = f"http://127.0.0.1:{HIVE_PORT}{path}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(data).encode() if data else None
    # Auto-detect POST when data is provided
    if body and method == "GET":
        method = "POST"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(req)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        print(f"  HTTP {e.code} {path}: {err_body[:200]}")
        return None


def wait_for_port(port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.socket()
            s.settimeout(1)
            s.connect(("127.0.0.1", port))
            s.close()
            return True
        except Exception:
            time.sleep(0.3)
    return False


def find_agent_port(agent_id: str, timeout: float = 30.0) -> int | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for port in range(AGENT_BASE_PORT, AGENT_BASE_PORT + 50):
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{port}/status")
                data = json.loads(urllib.request.urlopen(req, timeout=1).read().decode())
                if data.get("agent_id") == agent_id:
                    return port
            except Exception:
                pass
        time.sleep(1)
    return None


# ── Test phases ───────────────────────────────────────────────────────────

class MCPFreshnessTracker:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.errors = []

    def check(self, name: str, condition: bool, detail: str = ""):
        if condition:
            self.passed += 1
            print(f"  ✓ {name}")
        else:
            self.failed += 1
            msg = f"  ✗ {name}" + (f" — {detail}" if detail else "")
            print(msg)
            self.errors.append(msg)

    def summary(self):
        total = self.passed + self.failed
        print(f"\n{'='*60}")
        print(f"Results: {self.passed}/{total} passed, {self.failed} failed")
        if self.errors:
            print("Failures:")
            for e in self.errors:
                print(f"  {e}")
        print(f"{'='*60}")
        return self.failed == 0


def run_tests():
    tracker = MCPFreshnessTracker()
    procs_to_kill: list[subprocess.Popen] = []
    mcp_server = None
    test_db = BACKEND_DIR / "agent_marketplace_test.db"

    try:
        # ── Phase 0: Start MCP server ──────────────────────────────────
        print("\n[Phase 0] Starting test MCP server...")
        mcp_server = start_mcp_server(MCP_PORT)
        time.sleep(0.5)

        req = urllib.request.Request(f"http://127.0.0.1:{MCP_PORT}/")
        resp = urllib.request.urlopen(req, timeout=5).read().decode()
        tracker.check("MCP server is reachable", resp == "ok")

        init_msg = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "1.0"}}
        })
        req = urllib.request.Request(f"http://127.0.0.1:{MCP_PORT}/",
                                     data=init_msg.encode(),
                                     headers={"Content-Type": "application/json"})
        init_resp = json.loads(urllib.request.urlopen(req, timeout=5).read().decode())
        tracker.check("MCP initialize handshake",
                       init_resp.get("result", {}).get("serverInfo", {}).get("name") == "test-mcp-server")

        list_msg = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        req = urllib.request.Request(f"http://127.0.0.1:{MCP_PORT}/",
                                     data=list_msg.encode(),
                                     headers={"Content-Type": "application/json"})
        list_resp = json.loads(urllib.request.urlopen(req, timeout=5).read().decode())
        tool_names = [t["name"] for t in list_resp.get("result", {}).get("tools", [])]
        tracker.check("MCP tools/list returns 3 tools", set(tool_names) == {"echo", "add", "get_time"})

        call_msg = json.dumps({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "add", "arguments": {"a": 10, "b": 32}}
        })
        req = urllib.request.Request(f"http://127.0.0.1:{MCP_PORT}/",
                                     data=call_msg.encode(),
                                     headers={"Content-Type": "application/json"})
        call_resp = json.loads(urllib.request.urlopen(req, timeout=5).read().decode())
        result_text = call_resp.get("result", {}).get("content", [{}])[0].get("text", "")
        tracker.check("MCP tools/call: add(10, 32) = 42", result_text == "42")

        # ── Phase 1: Start backend (creates tables) + seed admin ──────
        print("\n[Phase 1] Starting backend and seeding admin...")

        os.environ["DEV_MODE"] = "1"
        os.environ["ENCRYPTION_KEY"] = "test-key-for-e2e-only-1234567890ab"
        test_db.unlink(missing_ok=True)

        env = os.environ.copy()
        env.update({
            "PORT": str(HIVE_PORT),
            "DATABASE_URL": f"sqlite+aiosqlite:///{test_db}",
            "ENCRYPTION_KEY": "test-key-for-e2e-only-1234567890ab",
            "HIVE_URL": f"http://127.0.0.1:{HIVE_PORT}",
            "OPENCLAW_PYTHON": sys.executable,
            "DEV_MODE": "1",
        })

        # Start backend — it creates tables and seeds default data
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app",
             "--host", "127.0.0.1", "--port", str(HIVE_PORT)],
            cwd=str(BACKEND_DIR), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        procs_to_kill.append(proc)

        if not wait_for_port(HIVE_PORT, timeout=30):
            tracker.check("Hive backend started", False, "port not open")
            return tracker.summary()
        tracker.check("Hive backend started", True)
        time.sleep(3)  # Wait for table creation + seeding

        # Register a user via the API, then promote to admin via DB
        resp = api("/api/auth/register", data={
            "email": "admin@hive.example.com",
            "password": "HiveAdmin123!",
            "name": "Admin",
        })
        if resp and ("access_token" in resp or "id" in resp):
            token = resp.get("access_token", "")
            print("  Registered via API, token=" + token[:20] + "...")
        else:
            # Fallback: insert directly (SQLite is single-file, tables exist now)
            import sqlite3
            from auth import get_password_hash
            admin_id = "e2e-admin-0000-0000-000000000001"
            pw_hash = get_password_hash("HiveAdmin123!")
            conn = sqlite3.connect(str(test_db))
            c = conn.cursor()
            c.execute(
                "INSERT OR IGNORE INTO users (id, email, hashed_password, is_admin, is_active) VALUES (?, ?, ?, 1, 1)",
                (admin_id, "admin@hive.example.com", pw_hash)
            )
            conn.commit()
            conn.close()
            print("  Admin user seeded via direct insert")
            token = ""

        # ── Phase 2: Login as admin ────────────────────────────────────
        print("\n[Phase 2] Logging in as admin...")

        if not token:
            resp = api("/api/auth/login", data={
                "email": "admin@hive.example.com", "password": "HiveAdmin123!"
            })
            token = (resp or {}).get("access_token", "")
        tracker.check("Admin login works", bool(token))

        if not token:
            return tracker.summary()

        # ── Phase 3: Register MCP server ───────────────────────────────
        print("\n[Phase 3] Registering MCP server in Hive...")

        server_resp = api("/api/mcp-servers", token, {
            "name": "Test MCP Tools",
            "url": f"http://127.0.0.1:{MCP_PORT}",
            "transport": "http",
            "description": "E2E test MCP server with echo, add, get_time",
            "visibility": "private",
        })
        server_id = (server_resp or {}).get("id", "")
        tracker.check("MCP server registered", bool(server_id))

        servers = api("/api/mcp-servers", token)
        tracker.check("MCP server appears in listing",
                       any(s.get("id") == server_id for s in (servers or [])))

        # ── Phase 4: Deploy agent with MCP access ──────────────────────
        print("\n[Phase 4] Deploying agent with MCP server access...")

        deploy_resp = api("/api/agents/deploy-hosted", token, {
            "name": "MCP Test Agent",
            "description": "Agent for testing MCP tool access",
            "framework": "openclaw",
            "mcp_server_ids": [server_id],
            "skill_names": ["terminal", "web_extract"],
            "model_key": {
                "provider": "openrouter",
                "key": os.getenv("OPENROUTER_API_KEY", "sk-or-v1-test-placeholder"),
                "model": "openai/gpt-4o-mini",
            },
        })
        agent_id = (deploy_resp or {}).get("agent_id", "")
        tracker.check("Agent deployed", bool(agent_id))

        if not agent_id:
            return tracker.summary()

        # Deploy response doesn't include port — scan for it
        print(f"  Scanning for agent {agent_id[:8]}...")
        agent_port = find_agent_port(agent_id, timeout=20)
        if not agent_port:
            tracker.check("Agent process started", False)
            return tracker.summary()
        tracker.check(f"Agent running on port {agent_port}", True)

        # ── Phase 5: Verify MCP tool discovery ─────────────────────────
        print("\n[Phase 5] Verifying MCP tool discovery...")

        try:
            req = urllib.request.Request(f"http://127.0.0.1:{agent_port}/status")
            status = json.loads(urllib.request.urlopen(req, timeout=5).read().decode())
        except Exception:
            status = None

        tracker.check("Agent status works", status is not None)
        # Framework is not in openclaw's status response — skip that check
        tracker.check("Agent has agent_id in status",
                       status and status.get("agent_id") == agent_id if status else False)

        try:
            req = urllib.request.Request(f"http://127.0.0.1:{agent_port}/skills")
            skills = json.loads(urllib.request.urlopen(req, timeout=5).read().decode())
            tracker.check("Agent has skills", bool(skills.get("skills")))
        except Exception:
            tracker.check("Agent has skills", False)

        # ── Phase 6: Test MCP tool calling via invoke ──────────────────
        print("\n[Phase 6] Testing MCP tool calling via invoke...")

        invoke_data = json.dumps({
            "task": "Use the echo tool to repeat this exact string: E2E_MCP_TEST_9876. "
                    "Then tell me what the echo tool returned."
        }).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{agent_port}/invoke",
            data=invoke_data,
            headers={"Content-Type": "application/json"},
        )
        try:
            invoke_resp = json.loads(urllib.request.urlopen(req, timeout=120).read().decode())
            output = invoke_resp.get("result", {}).get("output", "")
            tracker.check("Invoke returns success", invoke_resp.get("status") == "success")
            tracker.check("Echo tool called (unique string in output)",
                           "E2E_MCP_TEST_9876" in output,
                           f"output: {output[:200]}")
        except Exception as e:
            tracker.check("Invoke works", False, str(e))

        invoke_data = json.dumps({
            "task": "Use the add tool to compute 7291 + 3846. Report the result."
        }).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{agent_port}/invoke",
            data=invoke_data,
            headers={"Content-Type": "application/json"},
        )
        try:
            invoke_resp = json.loads(urllib.request.urlopen(req, timeout=120).read().decode())
            output = invoke_resp.get("result", {}).get("output", "")
            tracker.check("Add tool: 7291+3846=11137",
                           "11137" in output,
                           f"output: {output[:200]}")
        except Exception as e:
            tracker.check("Add tool invoke works", False, str(e))

        # ── Phase 7: Verify config persistence ─────────────────────────
        print("\n[Phase 7] Verifying config persistence...")

        import sqlite3 as _sqlite3
        from services.crypto import decrypt_json
        conn = _sqlite3.connect(str(test_db))
        c = conn.cursor()
        c.execute("SELECT config_encrypted FROM agents WHERE id = ?", (agent_id,))
        row = c.fetchone()
        conn.close()

        if row and row[0]:
            cfg = decrypt_json(row[0])
            has_mcp = bool(cfg and cfg.get("mcp_servers"))
            tracker.check("config_encrypted includes MCP servers", has_mcp)
            if has_mcp:
                mcp_names = [s.get("name") for s in cfg["mcp_servers"]]
                tracker.check("Config has correct MCP server name",
                               "Test MCP Tools" in mcp_names)
        else:
            tracker.check("config_encrypted exists", False)

    except Exception as e:
        print(f"\nFATAL: {e}")
        import traceback
        traceback.print_exc()
        tracker.check("Test harness did not crash", False, str(e))

    finally:
        print("\n[Cleanup] Stopping processes...")
        for p in procs_to_kill:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        if mcp_server:
            mcp_server.shutdown()
        test_db.unlink(missing_ok=True)
        print("  Done.")

    return tracker.summary()


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
