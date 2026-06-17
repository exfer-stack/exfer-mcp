"""Address-side tools: ``generate_address``, ``list_addresses``,
``get_balance``, and the chain-tip helper ``get_block_height``.

All are read-scope from walletd's perspective (``get_balance`` /
``list_addresses`` / ``get_block_height`` are strictly read;
``generate_address`` writes a key into the keystore but spends nothing).
An MCP host wiring this server is implicitly granting the agent the
ability to create new addresses for inbound payments — that's the
expected pattern.
"""

from __future__ import annotations

from typing import Any

import mcp.types as mcp_types
from exfer import AsyncClient

from ..config import Config
from ._common import json_text, render_error

GENERATE_ADDRESS_TOOL = mcp_types.Tool(
    name="exfer_generate_address",
    description=(
        "Create a new managed wallet address on this walletd. Returns "
        "{address, pubkey, index} as JSON — use the pubkey (64 hex) as "
        "payee_pubkey for exfer_quote_issue. Call this when the agent needs "
        "an address to receive a payment to."
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
        res = await client.generate_address()
    except Exception as exc:
        return render_error(exc)
    return [
        json_text(
            {
                "address": res["address"],
                "pubkey": res["pubkey"],
                "index": res["index"],
            }
        )
    ]


LIST_ADDRESSES_TOOL = mcp_types.Tool(
    name="exfer_list_addresses",
    description=(
        "List every address this walletd holds a key for, sorted ascending. "
        "Returns a JSON array of records, each {address, index, and one of "
        "label/imported}. There is no distinguished 'default' address — any of "
        "them can receive funds; to receive into one, pick any (e.g. the first, "
        "or one with a meaningful `label`), or call exfer_generate_address for a "
        "fresh one. Read-only."
    ),
    inputSchema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
)


async def list_addresses(
    client: AsyncClient,
    arguments: dict[str, Any],
    config: Config,
) -> list[mcp_types.TextContent]:
    del arguments, config
    try:
        records = await client.list_addresses()
    except Exception as exc:
        return render_error(exc)
    return [json_text(records)]


GET_BLOCK_HEIGHT_TOOL = mcp_types.Tool(
    name="exfer_get_block_height",
    description=(
        "Get the current chain tip height. Returns JSON {height, block_id} "
        "(height is an integer; block_id is the tip block hash as 64 hex). "
        "Read-only — use this instead of guessing the current height for "
        "HTLC timeouts."
    ),
    inputSchema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
)


async def get_block_height(
    client: AsyncClient,
    arguments: dict[str, Any],
    config: Config,
) -> list[mcp_types.TextContent]:
    del arguments, config
    try:
        tip = await client.get_tip()
    except Exception as exc:
        return render_error(exc)
    return [json_text({"height": tip.height, "block_id": tip.block_id})]


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
