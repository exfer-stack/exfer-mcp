"""``generate_address`` + ``get_balance`` — the two address-side tools.

Both are read-scope from walletd's perspective (``get_balance`` is
strictly read; ``generate_address`` writes a key into the keystore but
spends nothing). An MCP host wiring this server is implicitly granting
the agent the ability to create new addresses for inbound payments —
that's the expected pattern.
"""

from __future__ import annotations

from typing import Any

import mcp.types as mcp_types
from exfer_walletd import AsyncClient

from ..config import Config
from ._common import json_text, render_error, text

GENERATE_ADDRESS_TOOL = mcp_types.Tool(
    name="exfer_generate_address",
    description=(
        "Create a new managed wallet address on this walletd. Returns the "
        "address as a 64-char lowercase hex string. Use this when the agent "
        "needs an address to receive a payment to."
    ),
    inputSchema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
)


async def generate_address(
    client: AsyncClient,
    arguments: dict[str, Any],
    config: Config,
) -> list[mcp_types.TextContent]:
    del arguments, config
    try:
        addr = await client.generate_address()
    except Exception as exc:
        return render_error(exc)
    return [text(addr)]


GET_BALANCE_TOOL = mcp_types.Tool(
    name="exfer_get_balance",
    description=(
        "Look up the confirmed balance of a managed address, in exfers "
        "(1 EXFER = 100_000_000 exfers). Returns a single integer."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "address": {
                "type": "string",
                "description": "32-byte address as 64 lowercase hex characters.",
                "pattern": "^[0-9a-f]{64}$",
            }
        },
        "required": ["address"],
        "additionalProperties": False,
    },
)


async def get_balance(
    client: AsyncClient,
    arguments: dict[str, Any],
    config: Config,
) -> list[mcp_types.TextContent]:
    del config
    try:
        bal = await client.get_balance(arguments["address"])
    except Exception as exc:
        return render_error(exc)
    return [json_text({"address": arguments["address"], "balance": bal})]
