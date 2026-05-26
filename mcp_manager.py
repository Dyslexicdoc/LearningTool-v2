"""
MCP (Model Context Protocol) client foundation.

This module provides:
  - Storage for user-configured MCP servers (URL/transport/auth)
  - Connection testing (list tools available on a server)
  - Tool listing across all configured servers

NOT YET IMPLEMENTED:
  - Wiring MCP tools into the LLM call loop (so nodes can actually invoke them)

The current LearningTool LLM flow uses an OpenAI-compatible API. Wiring MCP
tools into that flow requires either:
  (a) Augmenting the orchestrator's tool-calling pipeline, or
  (b) Implementing a tool-call → MCP-call → tool-result loop directly here.

Both are non-trivial because of streaming + provider-format variation. The
foundation here is intentionally scoped to "configure and verify" so that
the full integration can happen in a follow-up without rebuilding the basics.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Optional


logger = logging.getLogger(__name__)


class MCPServerConfig:
    """In-memory representation of an MCP server config."""

    def __init__(self, *, id: str, name: str, transport: str,
                 url: str = "", command: str = "", args: Optional[list] = None,
                 headers: Optional[dict] = None, enabled: bool = True):
        self.id = id
        self.name = name
        self.transport = transport          # "http" | "sse" | "stdio"
        self.url = url                      # for http/sse
        self.command = command              # for stdio
        self.args = args or []              # for stdio
        self.headers = headers or {}        # for http/sse
        self.enabled = enabled

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "transport": self.transport,
            "url": self.url, "command": self.command, "args": self.args,
            "headers": self.headers, "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MCPServerConfig":
        return cls(
            id=d["id"], name=d["name"], transport=d["transport"],
            url=d.get("url", ""), command=d.get("command", ""),
            args=d.get("args", []), headers=d.get("headers", {}),
            enabled=d.get("enabled", True),
        )


class MCPManager:
    """Persistent storage + lifecycle management for MCP server configs."""

    def __init__(self, settings_dir: Path):
        self._path = settings_dir / "mcp_servers.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._servers: dict[str, MCPServerConfig] = {}
        self._load()

    def _load(self):
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for d in data.get("servers", []):
                cfg = MCPServerConfig.from_dict(d)
                self._servers[cfg.id] = cfg
        except (json.JSONDecodeError, OSError, KeyError) as e:
            logger.warning(f"Could not load MCP server config: {e}")

    def _save(self):
        data = {"servers": [s.to_dict() for s in self._servers.values()]}
        self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def list_servers(self) -> list[dict]:
        return [s.to_dict() for s in self._servers.values()]

    def add(self, *, name: str, transport: str, url: str = "",
            command: str = "", args: Optional[list] = None,
            headers: Optional[dict] = None) -> dict:
        if transport not in ("http", "sse", "stdio"):
            raise ValueError(f"Unknown transport: {transport}")
        if transport in ("http", "sse") and not url:
            raise ValueError(f"{transport} transport requires a URL")
        if transport == "stdio" and not command:
            raise ValueError("stdio transport requires a command")
        cfg = MCPServerConfig(
            id=f"mcp_{uuid.uuid4().hex[:12]}",
            name=name or "Unnamed server",
            transport=transport,
            url=url, command=command, args=args or [], headers=headers or {},
        )
        self._servers[cfg.id] = cfg
        self._save()
        return cfg.to_dict()

    def update(self, server_id: str, fields: dict) -> dict:
        if server_id not in self._servers:
            raise KeyError(server_id)
        s = self._servers[server_id]
        for k in ("name", "transport", "url", "command", "args", "headers", "enabled"):
            if k in fields:
                setattr(s, k, fields[k])
        self._save()
        return s.to_dict()

    def remove(self, server_id: str):
        if server_id in self._servers:
            del self._servers[server_id]
            self._save()

    def get(self, server_id: str) -> Optional[MCPServerConfig]:
        return self._servers.get(server_id)


# ---- Connection testing ----

async def test_connection(cfg: MCPServerConfig, timeout: float = 10.0) -> dict:
    """Attempt to connect to an MCP server and list its tools.

    Returns: {ok: bool, tools: [...], error: str|None}
    """
    if cfg.transport not in ("http", "sse"):
        return {
            "ok": False,
            "tools": [],
            "error": f"Transport {cfg.transport!r} not yet supported by the test client. "
                     f"Use http or sse for now.",
        }

    try:
        import httpx
    except ImportError:
        return {"ok": False, "tools": [], "error": "httpx not installed"}

    # Minimal MCP request — initialize, then list tools.
    # We deliberately do NOT depend on the official `mcp` package yet; it pulls
    # heavy deps. The protocol is JSON-RPC over HTTP/SSE; we send one request.
    init_payload = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "LearningTool", "version": "v2"},
        },
    }
    list_payload = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    headers.update(cfg.headers)

    try:
        async with httpx.AsyncClient(timeout=timeout, headers=headers,
                                     follow_redirects=True) as client:
            # Most MCP HTTP servers accept a single POST with both messages batched,
            # but the spec allows separate requests. Try separate for compatibility.
            r1 = await client.post(cfg.url, json=init_payload)
            if r1.status_code >= 400:
                return {"ok": False, "tools": [],
                        "error": f"HTTP {r1.status_code} on initialize"}
            r2 = await client.post(cfg.url, json=list_payload)
            if r2.status_code >= 400:
                return {"ok": False, "tools": [],
                        "error": f"HTTP {r2.status_code} on tools/list"}

        try:
            body = r2.json()
        except Exception:
            return {"ok": False, "tools": [],
                    "error": "Server response was not valid JSON"}
        if "error" in body:
            return {"ok": False, "tools": [],
                    "error": f"MCP error: {body['error'].get('message', body['error'])}"}
        tools = (body.get("result") or {}).get("tools", []) or []
        # Slim the tools down for UI display
        slim = [{"name": t.get("name", ""), "description": t.get("description", "")[:200]}
                for t in tools]
        return {"ok": True, "tools": slim, "error": None}
    except httpx.TimeoutException:
        return {"ok": False, "tools": [],
                "error": f"Timed out after {timeout}s — server unreachable?"}
    except Exception as e:
        return {"ok": False, "tools": [], "error": f"{type(e).__name__}: {e}"}
