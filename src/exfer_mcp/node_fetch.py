"""Minimal async JSON-RPC client for the Exfer **node** (not walletd).

The wallet-side tools talk to ``exfer-walletd`` via the typed
:class:`exfer.AsyncClient`. The network / explorer tools added in
:mod:`exfer_mcp.tools.network` instead talk directly to the L1 node's
JSON-RPC surface (``get_node_info`` / ``get_block`` / ``get_transaction``),
which walletd does not re-expose. There is no node client in the SDK, so this
module provides a tiny one with the same conventions as
:mod:`exfer_mcp.walletd_fetch` / the tool error-hint style:

* read-only, no keys, no spend;
* honours the same ``EXFER_NODE_RPC`` env the managed walletd uses
  (comma-separated list → tried in order, first reachable wins, mirroring
  walletd's own round-robin / failover);
* maps a JSON-RPC ``error`` object onto :class:`NodeRpcError` carrying the
  numeric ``code`` so the tool layer can render an actionable hint;
* maps a transport failure (every endpoint unreachable) onto
  :class:`NodeUnreachableError` so the agent is told to fix ``EXFER_NODE_RPC``.
"""

from __future__ import annotations

import os

import httpx

# Kept in sync with config.DEFAULT_NODE_RPC: the project's own dedicated nodes,
# tried in order with failover. Imported lazily-by-value (not from config) to
# keep this module free of the walletd-config machinery — it is a pure node
# client. EXFER_NODE_RPC overrides it.
from .config import DEFAULT_NODE_RPC

# Per-RPC timeout (seconds). Honours EXFER_MCP_HTTPX_TIMEOUT like the rest of the
# server; falls back to a short-but-generous default for a read-only query.
_DEFAULT_TIMEOUT = 30.0


class NodeRpcError(RuntimeError):
    """A JSON-RPC ``error`` object returned by the node.

    Carries the numeric ``code`` (e.g. -32602 invalid params) alongside the
    message so the tool layer can branch on it the way ``render_error`` does
    for walletd codes.
    """

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class NodeUnreachableError(RuntimeError):
    """Every configured node endpoint was unreachable / errored at transport."""


def node_rpc_endpoints() -> list[str]:
    """The node RPC base URLs to try, in order.

    ``EXFER_NODE_RPC`` (comma-separated) overrides the shipped default. Each
    entry is stripped of surrounding whitespace and any trailing slash so a
    ``POST`` always lands on the bare base URL the node serves JSON-RPC on.
    """
    raw = os.environ.get("EXFER_NODE_RPC") or DEFAULT_NODE_RPC
    return [part.strip().rstrip("/") for part in raw.split(",") if part.strip()]


def _timeout() -> float:
    val = os.environ.get("EXFER_MCP_HTTPX_TIMEOUT")
    if not val:
        return _DEFAULT_TIMEOUT
    try:
        parsed = float(val)
    except ValueError:
        return _DEFAULT_TIMEOUT
    return parsed if parsed > 0 else _DEFAULT_TIMEOUT


async def node_rpc(method: str, params: object | None = None) -> object:
    """Call ``method`` on the first reachable configured node, returning its
    ``result``.

    Tries each endpoint in :func:`node_rpc_endpoints` until one answers at the
    transport layer. A node that answers but returns a JSON-RPC ``error`` is a
    definitive answer (bad params, not-found) — it is raised as
    :class:`NodeRpcError` and we do NOT fail over to another node (the next node
    would give the same error). Only transport failures roll over; if every
    endpoint fails to connect, :class:`NodeUnreachableError` is raised.
    """
    endpoints = node_rpc_endpoints()
    if not endpoints:
        raise NodeUnreachableError("no node RPC endpoint configured (set EXFER_NODE_RPC)")

    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    last_transport_err: Exception | None = None

    async with httpx.AsyncClient(timeout=_timeout()) as http:
        for url in endpoints:
            try:
                resp = await http.post(url, json=payload)
                resp.raise_for_status()
                body = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                # Transport / decode failure for THIS endpoint — try the next.
                last_transport_err = exc
                continue
            if isinstance(body, dict) and body.get("error"):
                err = body["error"]
                code = err.get("code", 0) if isinstance(err, dict) else 0
                message = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                raise NodeRpcError(int(code), str(message))
            if isinstance(body, dict) and "result" in body:
                return body["result"]
            # Malformed (no result, no error) — treat as a transport-class miss.
            last_transport_err = ValueError(
                f"node returned a malformed JSON-RPC response: {body!r}"
            )

    raise NodeUnreachableError(
        f"no Exfer node reachable among {endpoints} (last error: {last_transport_err})"
    )
