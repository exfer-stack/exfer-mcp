"""MCP server entrypoint.

Spawned by an MCP host (Claude Desktop, Claude Code, …) as a subprocess.
The host speaks the MCP wire protocol over our stdin / stdout via
:func:`mcp.server.stdio.stdio_server`.

We hold a single long-lived :class:`exfer_walletd.AsyncClient` for the
lifetime of the process — httpx pools connections, so reusing the
client across tool calls reuses the underlying TCP connection and TLS
session.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import mcp.types as mcp_types
from exfer_walletd import AsyncClient
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server

from ._version import __version__
from .config import Config, ConfigError, ManagedConfig, managed_mode_selected
from .tools import HANDLERS, TOOLS
from .walletd_supervisor import WalletdSupervisor

SERVER_NAME = "exfer-mcp"


def build_server(client: AsyncClient, config: Config) -> Server[None, Any]:
    """Wire the tool registry into a fresh :class:`mcp.server.Server`.

    Split from :func:`main` so tests can drive the same dispatch
    without going through stdio.
    """
    server: Server[None, Any] = Server(SERVER_NAME, version=__version__)

    @server.list_tools()
    async def _list_tools() -> list[mcp_types.Tool]:
        return TOOLS

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[mcp_types.TextContent]:
        handler = HANDLERS.get(name)
        if handler is None:
            # Surface as a typed CallToolResult error via the framework's
            # exception → isError path. The MCP SDK turns a raised
            # exception into a structured error visible to the agent.
            raise ValueError(f"unknown tool: {name}")
        return await handler(client, arguments, config)

    return server


def _resolve_config() -> tuple[Config, WalletdSupervisor | None]:
    """Pick the run mode and produce the effective Config.

    EXTERNAL mode (``WALLETD_URL`` set): unchanged — build Config from env.
    MANAGED mode (``WALLETD_URL`` unset): spawn + supervise a walletd, then
    build the effective Config against the spawned instance. Returns the
    supervisor too so the caller can guarantee teardown on every exit path.
    """
    if managed_mode_selected():
        managed = ManagedConfig.from_env()
        supervisor = WalletdSupervisor(managed)
        url, token = supervisor.start()
        return Config.for_managed(url, token), supervisor
    return Config.from_env(), None


async def _run() -> None:
    try:
        config, supervisor = _resolve_config()
    except ConfigError as exc:
        # Exit before opening stdio: the host will treat this as a hard
        # subprocess failure and surface our stderr to the operator.
        print(f"exfer-mcp: configuration error: {exc}", file=sys.stderr)
        sys.exit(2)

    client_kwargs: dict[str, Any] = {"timeout": config.httpx_timeout}
    if config.walletd_fingerprint is not None:
        client_kwargs["fingerprint"] = config.walletd_fingerprint

    try:
        async with AsyncClient(config.walletd_url, config.walletd_token, **client_kwargs) as client:
            server = build_server(client, config)
            async with stdio_server() as (read_stream, write_stream):
                await server.run(
                    read_stream,
                    write_stream,
                    InitializationOptions(
                        server_name=SERVER_NAME,
                        server_version=__version__,
                        capabilities=server.get_capabilities(
                            notification_options=NotificationOptions(),
                            experimental_capabilities={},
                        ),
                    ),
                )
    finally:
        # MANAGED mode: never leave an orphaned walletd. Idempotent with
        # the supervisor's atexit + signal handlers.
        if supervisor is not None:
            supervisor.stop()


def main() -> None:
    """``[project.scripts]`` entrypoint. Synchronous wrapper around the
    async server loop so the binstub can be invoked directly by the MCP
    host without an event loop already running."""
    asyncio.run(_run())


if __name__ == "__main__":
    main()
