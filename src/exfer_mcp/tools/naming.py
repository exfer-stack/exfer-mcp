"""Human-readable names — a highest-cumulative-burn registry.

A name maps to a derived burn-script. Claiming a name means burning value
to that script; the party with the **highest cumulative burn** owns the
name and can be out-bid at any time. The owner declares where the name
points (default: themselves). Resolution returns the current pointer.

`resolve_name` is indexer-backed (walletd needs `--indexer-rpc`).
`name_claim` spends funds (the burn is unrecoverable). `name_script` is pure.
"""

from __future__ import annotations

from typing import Any

import mcp.types as mcp_types
from exfer_walletd import AsyncClient

from ..config import Config
from ._common import json_text, render_error

RESOLVE_NAME_TOOL = mcp_types.Tool(
    name="exfer_resolve_name",
    description=(
        "Resolve a human-readable name to the address it points to. The owner "
        "is whoever has burned the most to the name (an open auction — names "
        "can be out-bid). Returns `{name, script, address, owner, "
        "total_burned, claim_tx_id, claim_height}`; `address`/`owner` are null "
        "if unclaimed. Use before paying a named counterparty. Requires an "
        "indexer-backed walletd."
    ),
    inputSchema={
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
        "additionalProperties": False,
    },
)

NAME_CLAIM_TOOL = mcp_types.Tool(
    name="exfer_name_claim",
    description=(
        "Claim (or out-bid for) a name by burning `amount` to its derived "
        "script. SPENDS FUNDS (the burn is unrecoverable). Ownership is the "
        "highest cumulative burn, so a larger amount buys more standing and a "
        "name can be taken by out-burning the current owner. `target` declares "
        "where the name should point (default: `from`)."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "from": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "amount": {
                "type": "integer",
                "minimum": 1,
                "description": "Value to burn (the bid). Default 1000.",
            },
            "target": {
                "type": "string",
                "description": "Address the name points to (default: from).",
                "pattern": "^[0-9a-f]{64}$",
            },
            "fee": {"type": "integer", "minimum": 0},
        },
        "required": ["name", "from"],
        "additionalProperties": False,
    },
)

NAME_SCRIPT_TOOL = mcp_types.Tool(
    name="exfer_name_script",
    description=(
        "Derive the burn-script a name maps to (pure, no network). Returns "
        "`{name, script}`. Mostly for inspection/debugging; claiming and "
        "resolving handle this for you."
    ),
    inputSchema={
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
        "additionalProperties": False,
    },
)


async def resolve_name(
    client: AsyncClient, arguments: dict[str, Any], config: Config
) -> list[mcp_types.TextContent]:
    del config
    try:
        result = await client.resolve_name(arguments["name"])
    except Exception as exc:
        return render_error(exc)
    return [json_text(result)]


async def name_claim(
    client: AsyncClient, arguments: dict[str, Any], config: Config
) -> list[mcp_types.TextContent]:
    del config
    try:
        result = await client.name_claim(
            arguments["name"],
            from_=arguments["from"],
            amount=arguments.get("amount", 1000),
            target=arguments.get("target"),
            fee=arguments.get("fee"),
        )
    except Exception as exc:
        return render_error(exc)
    return [json_text(result)]


async def name_script(
    client: AsyncClient, arguments: dict[str, Any], config: Config
) -> list[mcp_types.TextContent]:
    del config
    try:
        result = await client.name_script(arguments["name"])
    except Exception as exc:
        return render_error(exc)
    return [json_text(result)]
