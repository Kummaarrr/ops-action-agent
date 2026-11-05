"""The agent's side of MCP: start the five servers as stdio subprocesses, list their tools,
call them. One `Systems` object per run; the harness resets the company between tasks and the
servers see the new state because they share the SQLite file.

    async with Systems.start(db_path) as systems:
        tools = systems.tool_definitions()   # Anthropic tool schemas, "store__get_order"
        result = await systems.call("store", "get_order", {"order_id": "O200001"})
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from opsagent.servers.catalog import SERVERS

SEP = "__"  # tool names are "<server>__<tool>" on the model side


@dataclass
class ToolResult:
    ok: bool
    data: Any
    text: str

    def for_model(self) -> str:
        if self.ok:
            return (
                json.dumps(self.data, default=str) if not isinstance(self.data, str) else self.data
            )
        return f"ERROR: {self.text}"


@dataclass
class Systems:
    sessions: dict[str, ClientSession]
    tools: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # server -> MCP tool dicts

    @classmethod
    @asynccontextmanager
    async def start(
        cls, db_path: Path | str, servers: tuple[str, ...] = SERVERS
    ) -> AsyncIterator[Systems]:
        env = {**os.environ, "OPSAGENT_DB": str(db_path)}
        # the servers import opsagent: make the source tree visible even where the package is
        # not installed (the hosted demo runs from a checkout)
        src = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
        async with AsyncExitStack() as stack:
            sessions: dict[str, ClientSession] = {}
            for name in servers:
                params = StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "opsagent.servers.mcp_servers", name],
                    env=env,
                )
                read, write = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                sessions[name] = session
            systems = cls(sessions)
            for name, session in sessions.items():
                listed = await session.list_tools()
                systems.tools[name] = [t.model_dump(by_alias=True) for t in listed.tools]
            yield systems

    def tool_definitions(self, allowed: set[str] | None = None) -> list[dict[str, Any]]:
        """Anthropic-format tool definitions, optionally restricted to a set of qualified names
        ("server.tool") - the guard's permission scoping."""
        out = []
        for server, tools in self.tools.items():
            for t in tools:
                qualified = f"{server}.{t['name']}"
                if allowed is not None and qualified not in allowed:
                    continue
                out.append(
                    {
                        "name": f"{server}{SEP}{t['name']}",
                        "description": t.get("description", ""),
                        "input_schema": t["inputSchema"],
                    }
                )
        return out

    @staticmethod
    def split(model_tool_name: str) -> tuple[str, str]:
        server, _, tool = model_tool_name.partition(SEP)
        return server, tool

    async def call(self, server: str, tool: str, arguments: dict[str, Any]) -> ToolResult:
        result = await self.sessions[server].call_tool(tool, arguments)
        text = "\n".join(
            getattr(c, "text", "")
            for c in getattr(result, "content", [])
            if getattr(c, "type", "") == "text"
        )
        if getattr(result, "is_error", False):
            return ToolResult(False, None, text or "tool error")
        structured = getattr(result, "structured_content", None)
        if structured is not None:
            data = (
                structured.get("result", structured) if isinstance(structured, dict) else structured
            )
            return ToolResult(True, data, text)
        try:
            return ToolResult(True, json.loads(text), text)
        except (json.JSONDecodeError, TypeError):
            return ToolResult(True, text, text)
