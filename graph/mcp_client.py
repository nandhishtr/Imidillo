"""
MCP client wiring for the single-agent Magdeburg Assistant.

Launches the four FastMCP stdio servers under `mcp_servers/` and exposes their
tools as LangChain `BaseTool`s, bound to PERSISTENT sessions — one long-lived
subprocess per server — so we never pay the subprocess-spawn + heavy-import cost
on every tool call (which is what `MultiServerMCPClient.get_tools()` does by
default: "a new session will be created for each tool call").

Self-healing: each server is owned by a `_ServerConnection` that can tear its
session down and respawn it. The tools handed to the agent are thin wrappers
(`_ResilientMCPTool`) that resolve the *current* underlying tool at call time;
if a call fails with a transport error (the subprocess died / the stdio pipe
closed), the wrapper reconnects just that one server and retries once. Without
this, a single crashed subprocess would break every later call to that server
for the whole process lifetime — and since `build_graph` has no in-process tool
fallback, that silently degrades chat until a full restart.

Lifecycle: the caller owns an `AsyncExitStack` whose lifetime == the app's. We
register a single teardown callback that closes every server connection. Closing
the stack tears all subprocesses down.
"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import AsyncExitStack
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# server name -> module file under mcp_servers/
_SERVERS = {
    "neo4j": "neo4j_server.py",
    "fiware": "fiware_server.py",
    "routing": "routing_server.py",
    "context": "context_server.py",
}


def build_connections(python_exe: str | None = None) -> dict[str, dict]:
    """Build the MultiServerMCPClient connection map for the 4 stdio servers."""
    py = python_exe or sys.executable
    conns: dict[str, dict] = {}
    for name, fname in _SERVERS.items():
        conns[name] = {
            "transport": "stdio",
            "command": py,
            "args": [os.path.join(_PROJECT_ROOT, "mcp_servers", fname)],
            "cwd": _PROJECT_ROOT,
            "env": dict(os.environ),   # propagate PATH + .env-derived vars
            "encoding": "utf-8",       # servers emit unicode (→, ß); avoid cp1252
        }
    return conns


def _is_transport_error(exc: Exception) -> bool:
    """True if `exc` looks like a dead/broken MCP transport (subprocess gone,
    stdio pipe closed) rather than an application/validation error a retry can't
    fix. Reconnecting only helps for the former.
    """
    # Bad params / schema mismatches won't be fixed by reconnecting.
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return False
    blob = f"{type(exc).__name__} {exc}".lower()
    markers = (
        "closedresource",      # anyio.ClosedResourceError
        "brokenresource",      # anyio.BrokenResourceError
        "endofstream",         # anyio.EndOfStream
        "broken pipe",
        "brokenpipe",
        "connection reset",
        "connection closed",
        "connectionerror",
        "process exited",
        "process is not running",
        "no such process",
        "stdio",
    )
    return any(m in blob for m in markers)


class _ServerConnection:
    """Owns one stdio server's session and can respawn it on demand.

    A per-connection lock serializes (re)connects so concurrent in-flight tool
    calls that all hit a dead session don't each spawn a fresh subprocess. A
    monotonically increasing `generation` lets a caller request "reconnect only
    if nobody has already reconnected since I last looked".
    """

    def __init__(self, client: MultiServerMCPClient, name: str):
        self._client = client
        self._name = name
        self._lock = asyncio.Lock()
        self._stack: AsyncExitStack | None = None
        self._session: Any = None
        self._tools_by_name: dict[str, BaseTool] = {}
        self._tool_meta: list[tuple[str, str, Any]] = []
        self.generation = 0

    async def open(self) -> None:
        """Open the session if it isn't already open (idempotent)."""
        if self._session is not None:
            return
        async with self._lock:
            if self._session is None:
                await self._open_locked()

    async def _open_locked(self) -> None:
        stack = AsyncExitStack()
        try:
            session = await stack.enter_async_context(self._client.session(self._name))
            tools = await load_mcp_tools(session)
        except BaseException:
            await stack.aclose()
            raise
        self._stack = stack
        self._session = session
        self._tools_by_name = {t.name: t for t in tools}
        # Capture tool metadata once so the agent-facing wrappers keep a stable
        # name/description/args_schema even across reconnects.
        if not self._tool_meta:
            self._tool_meta = [
                (t.name, t.description, getattr(t, "args_schema", None)) for t in tools
            ]

    async def _close_locked(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except BaseException:
                pass  # the subprocess may already be gone; tearing down is best-effort
        self._stack = None
        self._session = None
        self._tools_by_name = {}

    async def reconnect(self, seen_generation: int) -> None:
        """Respawn the subprocess + session, unless another caller already did
        so since `seen_generation` was observed."""
        async with self._lock:
            if self.generation != seen_generation:
                return  # someone else already healed this server
            print(f"[MCP] '{self._name}' transport lost — reconnecting...", flush=True)
            await self._close_locked()
            await self._open_locked()
            self.generation += 1
            print(f"[MCP] '{self._name}' reconnected (gen {self.generation})", flush=True)

    async def aclose(self) -> None:
        async with self._lock:
            await self._close_locked()

    @property
    def tool_meta(self) -> list[tuple[str, str, Any]]:
        return self._tool_meta

    async def call(self, tool_name: str, tool_input: dict) -> Any:
        """Invoke `tool_name` on the live session, healing a dead transport once."""
        await self.open()
        gen = self.generation
        underlying = self._tools_by_name.get(tool_name)
        if underlying is None:
            await self.reconnect(gen)
            underlying = self._tools_by_name.get(tool_name)
            if underlying is None:
                raise RuntimeError(f"MCP tool '{tool_name}' unavailable on '{self._name}'")
        try:
            return await underlying.ainvoke(tool_input)
        except Exception as exc:  # noqa: BLE001 — CancelledError (BaseException) still propagates
            if not _is_transport_error(exc):
                raise
            await self.reconnect(gen)
            underlying = self._tools_by_name.get(tool_name)
            if underlying is None:
                raise
            return await underlying.ainvoke(tool_input)


class _ResilientMCPTool(BaseTool):
    """Agent-facing tool that delegates to its server connection's current
    underlying tool, so reconnects are transparent to the agent."""

    conn: Any = None
    tool_name: str = ""

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("MCP tools are async-only; use ainvoke().")

    async def _arun(self, *args: Any, **kwargs: Any) -> Any:
        kwargs.pop("run_manager", None)
        return await self.conn.call(self.tool_name, kwargs)


async def open_mcp_tools(
    exit_stack: AsyncExitStack,
    python_exe: str | None = None,
) -> tuple[list[BaseTool], dict[str, list[str]]]:
    """Open ONE persistent, self-healing session per server and return
    (combined_tools, per_server_tool_names). Sessions stay alive until the
    caller closes `exit_stack`; a subprocess that dies mid-run is transparently
    respawned on the next tool call to that server.
    """
    client = MultiServerMCPClient(build_connections(python_exe))
    connections: dict[str, _ServerConnection] = {}
    tools: list[BaseTool] = []
    per_server: dict[str, list[str]] = {}

    async def _close_all() -> None:
        await asyncio.gather(
            *(c.aclose() for c in connections.values()), return_exceptions=True
        )

    exit_stack.push_async_callback(_close_all)

    for name in _SERVERS:
        conn = _ServerConnection(client, name)
        await conn.open()  # strict at startup: if a server can't boot, fail loudly
        connections[name] = conn
        names: list[str] = []
        for tool_name, description, args_schema in conn.tool_meta:
            wrapper = _ResilientMCPTool(
                name=tool_name,
                description=description,
                args_schema=args_schema,
                conn=conn,
                tool_name=tool_name,
            )
            tools.append(wrapper)
            names.append(tool_name)
        per_server[name] = names

    print(f"[MCP] persistent sessions up (self-healing); tools per server: {per_server}")
    return tools, per_server


if __name__ == "__main__":
    async def _selftest() -> None:
        async with AsyncExitStack() as stack:
            tools, per_server = await open_mcp_tools(stack)
            print(f"[MCP] total {len(tools)} tools loaded across {len(per_server)} servers")
            for name, names in per_server.items():
                print(f"  {name}: {names}")
            # sanity: invoke one cheap read tool through the live session
            by_name = {t.name: t for t in tools}
            if "list_entity_types" in by_name:
                out = await by_name["list_entity_types"].ainvoke({})
                print("\n[MCP] sample list_entity_types() call ->", str(out)[:300])

    asyncio.run(_selftest())
