"""Counterparty history — a raw, indexer-backed activity view.

`get_address_history` reads an indexer-backed timeline of *any* address (not
just managed ones) so an agent can inspect a counterparty's on-chain activity
before transacting. It is RAW data the caller interprets — deliberately NOT
presented as a "reputation score". Derived trust signals that can be cheaply
washed (e.g. self-dealt HTLC claim/refund outcomes) give false confidence, so a
real, cost-bound reputation tool is deferred until it cannot be faked for free
(the indexer still computes those edges; we just don't expose them to agents as
a trust score yet). Requires walletd started with an indexer (`--indexer-rpc`);
without one this returns an `IndexerNotConfigured` error.
"""

from __future__ import annotations

from typing import Any

import mcp.types as mcp_types
from exfer import AsyncClient

from ..config import Config
from ._common import json_text, render_error

GET_ADDRESS_HISTORY_TOOL = mcp_types.Tool(
    name="exfer_get_address_history",
    description=(
        "Activity timeline for any address — the inputs and outputs it appears "
        "in, with counterparties and amounts. Paginated. Raw on-chain history to "
        "inspect a counterparty before transacting (you interpret it; it is not a "
        "trust score). Requires an indexer-backed walletd."
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
