"""``wait_for_tx`` / ``wait_for_payment`` — event-driven waits, no polling.

``wait_for_tx`` blocks until a tx reaches a confirmation depth.
``wait_for_payment`` blocks until a *new* credit reaches an address and
returns the instant the paying tx is seen in the node's mempool —
sub-second when the node's SSE push is connected. Both subscribe to
walletd's internal channels (block-follower tip + the node SSE bus), so
they wake on the event rather than polling. Timeouts are clamped
server-side at 600 s.
"""

from __future__ import annotations

from typing import Any

import mcp.types as mcp_types
from exfer import AsyncClient

from ..config import Config
from ._common import json_text, render_error

WAIT_FOR_TX_TOOL = mcp_types.Tool(
    name="exfer_wait_for_tx",
    description=(
        "Wait until `tx_id` is buried at least `min_confirmations` blocks "
        "deep, or `timeout_secs` elapses. Returns `{tx_id, block_id, "
        "block_height, confirmations}` on success. Timing out is NOT a "
        "terminal failure — the tx may still confirm later; the tool surfaces "
        "the timeout as an error message and the agent can retry with a "
        "longer `timeout_secs`."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "tx_id": {
                "type": "string",
                "description": "Transaction id (64 lowercase hex).",
                "pattern": "^[0-9a-f]{64}$",
            },
            "min_confirmations": {
                "type": "integer",
                "description": "Default 1. Most agent payments are safe at 1.",
                "minimum": 1,
                "default": 1,
            },
            "timeout_secs": {
                "type": "integer",
                "description": "Default 60 s, clamped server-side at 600 s.",
                "minimum": 1,
                "maximum": 600,
                "default": 60,
            },
        },
        "required": ["tx_id"],
        "additionalProperties": False,
    },
)


async def wait_for_tx(
    client: AsyncClient,
    arguments: dict[str, Any],
    config: Config,
) -> list[mcp_types.TextContent]:
    del config
    try:
        result = await client.wait_for_tx(
            arguments["tx_id"],
            min_confirmations=arguments.get("min_confirmations", 1),
            timeout_secs=arguments.get("timeout_secs", 60),
        )
    except Exception as exc:
        return render_error(exc)
    return [json_text(result)]


WAIT_FOR_PAYMENT_TOOL = mcp_types.Tool(
    name="exfer_wait_for_payment",
    description=(
        "Wait for an incoming payment to `address`. Returns the moment a "
        "new credit of at least `min_amount` is seen in the node's mempool "
        "(0 confirmations) — sub-second when the node's SSE push is "
        "connected, so a selling agent can release a good/service instantly. "
        "On success returns `{received: true, tx_id, amount, confirmations, "
        "tip_height}`. This is a receipt/liveness signal, NOT settlement "
        "finality — for final settlement follow up with `exfer_wait_for_tx`. "
        "A quiet window is not an error: on timeout it returns `{received: "
        "false, timed_out: true}` and the agent can simply call again."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "address": {
                "type": "string",
                "description": "Receiving address to watch (64 lowercase hex).",
                "pattern": "^[0-9a-f]{64}$",
            },
            "min_amount": {
                "type": "integer",
                "description": (
                    "Default 1. Only report a credit of at least this many "
                    "exfers (1 EXFER = 100_000_000 exfers)."
                ),
                "minimum": 1,
                "default": 1,
            },
            "timeout_secs": {
                "type": "integer",
                "description": "Default 60 s, clamped server-side at 600 s.",
                "minimum": 1,
                "maximum": 600,
                "default": 60,
            },
        },
        "required": ["address"],
        "additionalProperties": False,
    },
)


async def wait_for_payment(
    client: AsyncClient,
    arguments: dict[str, Any],
    config: Config,
) -> list[mcp_types.TextContent]:
    del config
    try:
        result = await client.wait_for_payment(
            arguments["address"],
            min_amount=arguments.get("min_amount", 1),
            timeout_secs=arguments.get("timeout_secs", 60),
        )
    except Exception as exc:
        return render_error(exc)
    return [json_text(result)]
