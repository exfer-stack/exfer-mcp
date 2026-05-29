"""``wait_for_tx`` — block until a tx has the requested confirmation depth.

Walletd's ``wait_for_tx`` subscribes to its block-follower watch channel,
so this tool wakes up the moment the tip advances past the target — no
polling, no fixed sleeps. Timeout is clamped server-side at 600 s.
"""

from __future__ import annotations

from typing import Any

import mcp.types as mcp_types
from exfer_walletd import AsyncClient

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
