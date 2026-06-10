"""Shared tool-handler plumbing.

Every tool handler accepts an :class:`exfer.AsyncClient`, a
``dict[str, Any]`` of validated arguments, and a :class:`~..config.Config`
(for defaults the agent didn't supply), and returns a list of
``TextContent`` blocks. Errors from walletd are caught here and
rendered as ``isError=True`` content the agent can read and react to.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import mcp.types as mcp_types
from exfer import (
    AsyncClient,
    ExferError,
    IndexerNotConfiguredError,
    InsufficientBalanceError,
    WaitTimeoutError,
)

from ..config import Config

# Each tool handler is `(client, args, config) -> list[TextContent]`.
ToolHandler = Callable[
    [AsyncClient, dict[str, Any], Config],
    Awaitable[list[mcp_types.TextContent]],
]


def text(s: str) -> mcp_types.TextContent:
    """Wrap a string in the MCP TextContent envelope."""
    return mcp_types.TextContent(type="text", text=s)


def json_text(result: object) -> mcp_types.TextContent:
    """Serialize an SDK result to indented JSON for the agent to read.

    Indented because Claude (and other LLM agents) parse pretty-printed
    JSON noticeably more reliably than minified — small token cost,
    fewer downstream "what does this number mean?" round trips.
    """
    return text(json.dumps(result, indent=2, sort_keys=True))


_NODE_HINT = (
    " — the upstream Exfer node looks unreachable from here; set EXFER_NODE_RPC "
    "to a node you can reach (or run one locally), then retry."
)


def _looks_node_unreachable(exc: Exception) -> bool:
    """Heuristic: does this error mean walletd couldn't reach its upstream node?"""
    if getattr(exc, "code", None) == -32020:  # UpstreamUnreachable
        return True
    s = str(exc).lower()
    return any(
        k in s
        for k in (
            "upstream",
            "unreachable",
            "502",
            "503",
            "504",
            "connection refused",
            "econnrefused",
            "timed out",
            "no route",
            "getaddrinfo",
            "name resolution",
        )
    )


def render_error(exc: Exception) -> list[mcp_types.TextContent]:
    """Map a walletd-side exception to text the agent can act on.

    Recognised cases get a one-line plain-English summary plus the raw
    JSON-RPC code so a debugging operator can grep for it. Unrecognised
    errors get a generic envelope; the agent still sees the type name
    and stringified message.
    """
    if isinstance(exc, InsufficientBalanceError):
        flag = (
            " (some UTXOs are reserved by pending transfers; retry after they confirm)"
            if exc.in_flight_reserved
            else ""
        )
        return [
            text(
                f"insufficient balance to cover amount + fee{flag}. "
                f"walletd code {exc.code}: {exc.message}"
            )
        ]
    if isinstance(exc, WaitTimeoutError):
        return [
            text(
                f"transaction did not reach the requested confirmation depth "
                f"within the timeout. This is not a terminal failure — the tx "
                f"may still confirm; call wait_for_tx again with a longer "
                f"timeout_secs. walletd code {exc.code}: {exc.message}"
            )
        ]
    if isinstance(exc, IndexerNotConfiguredError):
        return [
            text(
                f"this query needs the indexer service, but walletd was not "
                f"started with --indexer-rpc. Operator action required. "
                f"walletd code {exc.code}: {exc.message}"
            )
        ]
    hint = _NODE_HINT if _looks_node_unreachable(exc) else ""
    if isinstance(exc, ExferError):
        code = getattr(exc, "code", None)
        if code is not None:
            return [text(f"walletd error (code {code}): {exc}{hint}")]
        return [text(f"walletd error: {exc}{hint}")]
    return [text(f"{type(exc).__name__}: {exc}{hint}")]
