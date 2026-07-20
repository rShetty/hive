"""MCP client for the Hive OpenClaw agent runtime.

Supports three transports:
  * "http"  — Streamable HTTP: JSON-RPC POSTs to ``url`` (parses an SSE
              response if the server streams one).
  * "sse"   — Server-Sent Events: GET ``url`` opens the server->client stream;
              the server advertises a POST ``endpoint`` via an ``endpoint``
              event, which we POST client->server JSON-RPC to.
  * "stdio" — launches ``command`` as a subprocess and speaks JSON-RPC over
              newline-delimited stdin/stdout.

The agent runtime aggregates tools across all configured servers and exposes
them to the LLM as OpenAI-style function tools.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, List, Optional


class MCPError(Exception):
    pass


def _parse_sse_lines(text: str):
    """Yield (event, data) pairs from a raw SSE chunk of text."""
    event = "message"
    data_lines: List[str] = []
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        if line == "":
            if data_lines:
                yield event, "\n".join(data_lines)
                event = "message"
                data_lines = []
        elif line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())
        elif line.startswith(":"):
            continue
    if data_lines:
        yield event, "\n".join(data_lines)


class MCPConnection:
    """A single MCP server connection (one transport)."""

    def __init__(self, cfg: Dict[str, Any]):
        self.name = cfg.get("name", "mcp")
        self.transport = (cfg.get("transport") or "http").lower()
        self.url = cfg.get("url", "")
        self.headers = cfg.get("headers") or {}
        self.command = cfg.get("command")
        self.env = cfg.get("env") or {}
        self._id = 0
        self._client: Optional[Any] = None
        self._session_id: Optional[str] = None
        self._endpoint: Optional[str] = None
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._sse_task: Optional[asyncio.Task] = None
        self._sse_event = "message"
        self._sse_queue: "asyncio.Queue[dict]" = asyncio.Queue()
        self._pending: Dict[int, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    async def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def _rpc(self, method: str, params: Optional[dict] = None) -> Any:
        rid = await self._next_id()
        payload = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[rid] = fut
        try:
            await self._send(payload)
            return await asyncio.wait_for(fut, timeout=30.0)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise MCPError(f"{self.name}: timeout on {method}")
        finally:
            self._pending.pop(rid, None)

    async def _send(self, payload: dict):
        if self.transport == "stdio":
            if not self._proc or self._proc.stdin is None:
                raise MCPError(f"{self.name}: stdio process not running")
            self._proc.stdin.write((json.dumps(payload) + "\n").encode())
            await self._proc.stdin.drain()
        else:
            await self._http_send(payload)

    async def _http_send(self, payload: dict):
        if self._client is None:
            raise MCPError(f"{self.name}: not connected")
        if self.transport == "sse":
            post_url = self._endpoint or self.url
            headers = {"Content-Type": "application/json", **self.headers}
            if self._session_id:
                headers["Mcp-Session-Id"] = self._session_id
            r = await self._client.post(post_url, json=payload, headers=headers)
            if r.status_code >= 400:
                raise MCPError(f"{self.name}: POST {payload['method']} -> {r.status_code}")
        else:
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                **self.headers,
            }
            if self._session_id:
                headers["Mcp-Session-Id"] = self._session_id
            r = await self._client.post(self.url, json=payload, headers=headers)
            if r.status_code >= 400:
                raise MCPError(f"{self.name}: POST {payload['method']} -> {r.status_code}")
            ctype = r.headers.get("content-type", "")
            if "text/event-stream" in ctype:
                self._dispatch_sse(r.text)
            elif r.text.strip():
                self._dispatch_json(r.json())

    def _dispatch_json(self, msg: dict):
        if "id" in msg and msg["id"] in self._pending:
            fut = self._pending[msg["id"]]
            if not fut.done():
                if "error" in msg:
                    fut.set_exception(MCPError(str(msg["error"])))
                else:
                    fut.set_result(msg.get("result"))

    def _dispatch_sse(self, text: str):
        for _event, data in _parse_sse_lines(text):
            try:
                msg = json.loads(data)
            except Exception:
                continue
            self._dispatch_json(msg)

    async def _sse_loop(self):
        assert self._client is not None
        headers = {"Accept": "text/event-stream", **self.headers}
        try:
            async with self._client.stream("GET", self.url, headers=headers) as resp:
                if "Mcp-Session-Id" in resp.headers:
                    self._session_id = resp.headers["Mcp-Session-Id"]
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if line.startswith("event:"):
                        self._sse_event = line[len("event:"):].strip()
                    elif line.startswith("data:"):
                        data = line[len("data:"):].strip()
                        if self._sse_event == "endpoint":
                            self._endpoint = data if data.startswith("http") else self.url.rstrip("/") + data
                        elif self._sse_event in ("message", ""):
                            try:
                                msg = json.loads(data)
                            except Exception:
                                continue
                            self._dispatch_json(msg)
                        self._sse_event = "message"
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[mcp:{self.name}] SSE loop error: {e}")

    async def connect(self):
        if self.transport == "stdio":
            await self._connect_stdio()
        else:
            import httpx
            self._client = httpx.AsyncClient(timeout=30.0)
            if self.transport == "sse":
                self._sse_event = "message"
                self._endpoint = None
                self._sse_task = asyncio.create_task(self._sse_loop())
                await asyncio.sleep(0.4)
        await self._initialize()

    async def _connect_stdio(self):
        if not self.command:
            raise MCPError(f"{self.name}: stdio transport requires a command")
        env = dict(os.environ)
        env.update({k: str(v) for k, v in self.env.items()})
        self._proc = await asyncio.create_subprocess_exec(
            *self.command.split(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        self._sse_task = asyncio.create_task(self._stdio_loop())

    async def _stdio_loop(self):
        assert self._proc and self._proc.stdout
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                line = line.decode().strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                self._dispatch_json(msg)
        except asyncio.CancelledError:
            pass

    async def _initialize(self):
        result = await self._rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "hive-openclaw", "version": "1.0"},
            },
        )
        if isinstance(result, dict) and result.get("sessionId"):
            self._session_id = result["sessionId"]
        try:
            await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception:
            pass

    async def list_tools(self) -> List[Dict[str, Any]]:
        result = await self._rpc("tools/list", {})
        if isinstance(result, dict):
            return result.get("tools", []) or []
        return []

    async def call_tool(self, tool_name: str, arguments: dict) -> Any:
        return await self._rpc("tools/call", {"name": tool_name, "arguments": arguments or {}})

    async def close(self):
        if self._sse_task:
            self._sse_task.cancel()
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
        if self._client:
            await self._client.aclose()
            self._client = None


class MCPManager:
    """Manages all configured MCP servers and aggregates their tools."""

    def __init__(self, servers: List[Dict[str, Any]]):
        self.servers = [MCPConnection(c) for c in servers]
        self._connected = False
        self.tool_index: Dict[str, tuple[MCPConnection, dict]] = {}
        self.status: List[Dict[str, Any]] = []

    async def connect_all(self):
        for s in self.servers:
            entry = {"name": s.name, "transport": s.transport, "connected": False,
                     "tools": 0, "error": None}
            try:
                await s.connect()
                tools = await s.list_tools()
                entry["connected"] = True
                entry["tools"] = len(tools)
                for t in tools:
                    qualified = f"{s.name}__{t.get('name')}"
                    self.tool_index[qualified] = (s, t)
            except Exception as e:
                entry["error"] = str(e)[:160]
            self.status.append(entry)
        self._connected = True

    def openai_tools(self) -> List[Dict[str, Any]]:
        out = []
        for qualified, (_conn, t) in self.tool_index.items():
            out.append({
                "type": "function",
                "function": {
                    "name": qualified,
                    "description": t.get("description", "") or f"Tool {t.get('name')}",
                    "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
                },
            })
        return out

    async def call(self, qualified_name: str, arguments: dict) -> str:
        item = self.tool_index.get(qualified_name)
        if not item:
            return f"[error] unknown tool: {qualified_name}"
        conn, _t = item
        try:
            res = await conn.call_tool(qualified_name.split("__", 1)[1], arguments)
        except Exception as e:
            return f"[error] {qualified_name} failed: {e}"
        return self._format_result(res)

    @staticmethod
    def _format_result(res: Any) -> str:
        if isinstance(res, str):
            return res
        if isinstance(res, dict):
            content = res.get("content")
            if isinstance(content, list):
                parts = []
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        parts.append(c.get("text", ""))
                    else:
                        parts.append(json.dumps(c))
                return "\n".join(parts)
            return json.dumps(res)
        return json.dumps(res)

    async def close_all(self):
        for s in self.servers:
            try:
                await s.close()
            except Exception:
                pass
