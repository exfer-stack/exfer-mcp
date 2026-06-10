"""MCP server entrypoint.

Spawned by an MCP host (Claude Desktop, Claude Code, …) as a subprocess.
The host speaks the MCP wire protocol over our stdin / stdout via
:func:`mcp.server.stdio.stdio_server`.

We hold a single long-lived :class:`exfer.AsyncClient` for the
lifetime of the process — httpx pools connections, so reusing the
client across tool calls reuses the underlying TCP connection and TLS
session.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from typing import Any

import mcp.types as mcp_types
from exfer import AsyncClient
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server

from ._version import __version__
from .config import Config, ConfigError, ManagedConfig, managed_mode_selected
from .tools import HANDLERS, NO_WALLETD_TOOLS, TOOLS
from .update_check import startup_notice
from .walletd_supervisor import WalletdSupervisor

# Surfaced to the host at the MCP handshake (InitializationOptions.instructions)
# — many hosts pass this to the model as context.
SERVER_INSTRUCTIONS = (
    "exfer-mcp gives you typed tools to operate an Exfer wallet. The wallet is "
    "spendable with NO per-transaction human approval, so confirm the amount and "
    "destination with the user before exfer_transfer / exfer_htlc_*. On first use, "
    "or whenever the user asks about updates, call exfer_check_update; if it reports "
    "update_available or current_version_yanked, tell the user and relay how_to_update. "
    "Updating never touches the wallet's keys."
)

SERVER_NAME = "exfer-mcp"

# A tool call's lazy gate: resolves the live ``(client, config)`` to use,
# awaiting managed walletd readiness on the first call. EXTERNAL mode's
# provider returns the eagerly-built client immediately.
ClientProvider = Callable[[], Awaitable[tuple[AsyncClient, Config]]]


def build_server(provider: ClientProvider) -> Server[None, Any]:
    """Wire the tool registry into a fresh :class:`mcp.server.Server`.

    Split from :func:`main` so tests can drive the same dispatch
    without going through stdio.

    ``provider`` is awaited only inside the tool-call path — never in
    ``list_tools`` — so the MCP handshake + tool listing stay
    non-blocking even while a managed walletd is still booting.
    """
    server: Server[None, Any] = Server(SERVER_NAME, version=__version__)

    @server.list_tools()
    async def _list_tools() -> list[mcp_types.Tool]:
        # Static tool definitions only — must NOT touch walletd, so the
        # handshake / tool listing returns with zero wait in managed mode.
        return TOOLS

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[mcp_types.TextContent]:
        handler = HANDLERS.get(name)
        if handler is None:
            # Surface as a typed CallToolResult error via the framework's
            # exception → isError path. The MCP SDK turns a raised
            # exception into a structured error visible to the agent.
            raise ValueError(f"unknown tool: {name}")
        if name in NO_WALLETD_TOOLS:
            # Pure / meta tools (e.g. exfer_check_update) need no walletd — skip
            # the readiness gate so they work even while walletd is down. These
            # handlers ignore the client/config arguments.
            return await handler(None, arguments, None)  # type: ignore[arg-type]
        # Lazy gate: in managed mode this awaits walletd readiness (only the
        # first call is slow; later calls return immediately). A readiness
        # failure raises ConfigError, which the SDK renders as a tool error
        # the agent can read — never a hang.
        client, config = await provider()
        return await handler(client, arguments, config)

    return server


def _external_provider(config: Config, client: AsyncClient) -> ClientProvider:
    """EXTERNAL mode: hand back the eagerly-built client, unchanged."""

    async def _provide() -> tuple[AsyncClient, Config]:
        return client, config

    return _provide


def _managed_provider(supervisor: WalletdSupervisor) -> ClientProvider:
    """MANAGED mode: await readiness, then build + cache the client lazily.

    The AsyncClient is only constructed after the background bring-up has
    produced the effective ``(url, token)`` — so nothing connects to a
    not-yet-listening walletd, and the client is reused across calls.
    """
    cached: dict[str, tuple[AsyncClient, Config]] = {}
    lock = asyncio.Lock()

    async def _provide() -> tuple[AsyncClient, Config]:
        url, token = await supervisor.ensure_ready()
        async with lock:
            if "c" not in cached:
                config = Config.for_managed(url, token)
                client_kwargs: dict[str, Any] = {"timeout": config.httpx_timeout}
                client = AsyncClient(url, token, **client_kwargs)
                cached["c"] = (client, config)
        return cached["c"]

    return _provide


async def _run() -> None:
    try:
        if managed_mode_selected():
            await _run_managed()
        else:
            await _run_external()
    except ConfigError as exc:
        # Exit before/at stdio open: the host treats this as a hard
        # subprocess failure and surfaces our stderr to the operator.
        print(f"exfer-mcp: configuration error: {exc}", file=sys.stderr)
        sys.exit(2)


async def _serve(provider: ClientProvider) -> None:
    """Open stdio and run the MCP server loop with the given client provider."""
    server = build_server(provider)
    # Best-effort, non-blocking: notify the operator (stderr) of a newer / yanked
    # release. Never blocks serving; never auto-applies. Held in a local so the
    # task isn't GC'd while pending.
    notice_task = asyncio.create_task(_emit_startup_update_notice())
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name=SERVER_NAME,
                server_version=__version__,
                instructions=SERVER_INSTRUCTIONS,
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )
    notice_task.cancel()


async def _emit_startup_update_notice() -> None:
    """Print a one-line stderr notice if an update is available / version yanked."""
    notice = await asyncio.to_thread(startup_notice)
    if notice:
        print(notice, file=sys.stderr, flush=True)


async def _run_external() -> None:
    """EXTERNAL mode (``WALLETD_URL`` set) — behaviour unchanged.

    Build the effective Config from env, open the long-lived AsyncClient
    eagerly, and serve. No walletd spawn, no readiness gating.
    """
    config = Config.from_env()
    client_kwargs: dict[str, Any] = {"timeout": config.httpx_timeout}
    if config.walletd_fingerprint is not None:
        client_kwargs["fingerprint"] = config.walletd_fingerprint

    async with AsyncClient(config.walletd_url, config.walletd_token, **client_kwargs) as client:
        await _serve(_external_provider(config, client))


async def _run_managed() -> None:
    """MANAGED mode (``WALLETD_URL`` unset) — LAZY / non-blocking.

    Choose the bind port synchronously (instant) and kick off the walletd
    boot in the BACKGROUND, then start serving immediately so the MCP
    handshake + list_tools respond with zero wait. The first tool call
    awaits :meth:`WalletdSupervisor.ensure_ready`. Teardown is guaranteed
    on every exit path (and idempotent with atexit + signal handlers), so
    a server that exits before walletd is ready still kills the spawning
    child — no orphan.
    """
    managed = ManagedConfig.from_env()
    supervisor = WalletdSupervisor(managed)
    try:
        supervisor.start_background()
        await _serve(_managed_provider(supervisor))
    finally:
        supervisor.stop()


def main() -> None:
    """``[project.scripts]`` entrypoint. Synchronous wrapper around the
    async server loop so the binstub can be invoked directly by the MCP
    host without an event loop already running."""
    asyncio.run(_run())


if __name__ == "__main__":
    main()
