"""Counterparty reputation / history — on-chain trust signals.

These read an indexer-backed view of *any* address (not just managed
ones), so an agent can vet a counterparty before transacting. They
require walletd to be started with an indexer (`--indexer-rpc`); without
one they return an `IndexerNotConfigured` error.
"""

from __future__ import annotations

from typing import Any

import mcp.types as mcp_types
from exfer_walletd import AsyncClient

from ..config import Config
from ._common import json_text, render_error

GET_ADDRESS_HISTORY_TOOL = mcp_types.Tool(
    name="exfer_get_address_history",
    description=(
        "Activity timeline for any address — the inputs and outputs it appears "
        "in, with counterparties and amounts. Paginated. Use to inspect a "
        "counterparty's on-chain history before transacting. Requires an "
        "indexer-backed walletd."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "address": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "since_height": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1},
            "cursor": {"type": "string"},
        },
        "required": ["address"],
        "additionalProperties": False,
    },
)

GET_ATTESTATION_EDGES_TOOL = mcp_types.Tool(
    name="exfer_get_attestation_edges",
    description=(
        "Per-counterparty reputation edges for an address: for each counterparty "
        "it ran contracts with, how many total / succeeded / refunded, and when "
        "last seen. The on-chain trust score to check before doing business with "
        "a counterparty. Requires an indexer-backed walletd."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "address": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "contract_hash": {
                "type": "string",
                "description": "Optional: restrict to one contract template.",
                "pattern": "^[0-9a-f]{64}$",
            },
        },
        "required": ["address"],
        "additionalProperties": False,
    },
)

DETECT_IN_CHAIN_SWAPS_TOOL = mcp_types.Tool(
    name="exfer_detect_in_chain_swaps",
    description=(
        "Groups of HTLCs sharing a hash_lock — the on-chain fingerprint of atomic "
        "swaps. Useful for auditing swap counterparties. Requires an "
        "indexer-backed walletd."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "hash_lock": {
                "type": "string",
                "description": "Optional: restrict to one hash_lock.",
                "pattern": "^[0-9a-f]{64}$",
            },
            "limit": {"type": "integer", "minimum": 1},
        },
        "additionalProperties": False,
    },
)


async def get_address_history(
    client: AsyncClient, arguments: dict[str, Any], config: Config
) -> list[mcp_types.TextContent]:
    del config
    try:
        result = await client.get_address_history(
            arguments["address"],
            since_height=arguments.get("since_height"),
            limit=arguments.get("limit"),
            cursor=arguments.get("cursor"),
        )
    except Exception as exc:
        return render_error(exc)
    return [json_text(result)]


async def get_attestation_edges(
    client: AsyncClient, arguments: dict[str, Any], config: Config
) -> list[mcp_types.TextContent]:
    del config
    try:
        result = await client.get_attestation_edges(
            arguments["address"], contract_hash=arguments.get("contract_hash")
        )
    except Exception as exc:
        return render_error(exc)
    return [json_text(result)]


async def detect_in_chain_swaps(
    client: AsyncClient, arguments: dict[str, Any], config: Config
) -> list[mcp_types.TextContent]:
    del config
    try:
        result = await client.detect_in_chain_swaps(
            hash_lock=arguments.get("hash_lock"), limit=arguments.get("limit")
        )
    except Exception as exc:
        return render_error(exc)
    return [json_text(result)]
