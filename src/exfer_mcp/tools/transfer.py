"""``simulate_transfer`` + ``transfer`` — the spend pair.

The agent is **strongly** expected to call ``simulate_transfer`` first
to surface the exact fee and inputs, then call ``transfer`` with the
same amount once it has confirmed the cost. Walletd will rebuild the
tx server-side; the simulate result is a preview, not a reservation.
"""

from __future__ import annotations

from typing import Any

import mcp.types as mcp_types
from exfer_walletd import AsyncClient

from ..config import Config
from ._common import json_text, render_error

SIMULATE_TRANSFER_TOOL = mcp_types.Tool(
    name="exfer_simulate_transfer",
    description=(
        "Dry-run a payment. Returns the exact (size, fee, fee_rate, inputs, "
        "outputs, change, built_at_height) tuple that a real `exfer_transfer` "
        "call would produce, without broadcasting and without reserving any "
        "UTXOs. ALWAYS call this BEFORE `exfer_transfer` so the user knows "
        "the cost ceiling in advance."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "from_address": {
                "type": "string",
                "description": "Sender — must be a managed address (a key walletd holds).",
                "pattern": "^[0-9a-f]{64}$",
            },
            "to_address": {
                "type": "string",
                "description": "Recipient address.",
                "pattern": "^[0-9a-f]{64}$",
            },
            "amount": {
                "type": "integer",
                "description": "Amount in exfers (1 EXFER = 100_000_000).",
                "minimum": 1,
            },
            "fee_rate": {
                "type": "integer",
                "description": (
                    "Optional fee rate in exfers/byte. Omit to use walletd's "
                    "default. Mutually exclusive with `fee`."
                ),
                "minimum": 1,
            },
            "fee": {
                "type": "integer",
                "description": (
                    "Optional absolute fee in exfers. Omit to let walletd "
                    "compute one from fee_rate. Mutually exclusive with "
                    "`fee_rate`."
                ),
                "minimum": 0,
            },
            "datum": {
                "type": "string",
                "description": (
                    "Optional hex (<= 4096 bytes, even length): the same "
                    "on-chain datum the real `exfer_transfer` would attach "
                    "(e.g. a quote_id for an honor settlement). Pass it here "
                    "so the dry-run counts the datum bytes toward size/fee and "
                    "matches the real transfer exactly."
                ),
                "pattern": "^([0-9a-f]{2})*$",
            },
        },
        "required": ["from_address", "to_address", "amount"],
        "additionalProperties": False,
    },
)


async def simulate_transfer(
    client: AsyncClient,
    arguments: dict[str, Any],
    config: Config,
) -> list[mcp_types.TextContent]:
    kwargs: dict[str, Any] = {
        "from_": arguments["from_address"],
        "outputs": [{"to": arguments["to_address"], "amount": arguments["amount"]}],
    }
    if "fee" in arguments:
        kwargs["fee"] = arguments["fee"]
    elif "fee_rate" in arguments:
        kwargs["fee_rate"] = arguments["fee_rate"]
    elif config.default_fee_rate is not None:
        kwargs["fee_rate"] = config.default_fee_rate
    if "datum" in arguments:
        kwargs["datum"] = arguments["datum"]

    try:
        result = await client.simulate_transfer(**kwargs)
    except Exception as exc:
        return render_error(exc)
    return [json_text(result)]


TRANSFER_TOOL = mcp_types.Tool(
    name="exfer_transfer",
    description=(
        "Build, sign, and broadcast a payment. This MOVES FUNDS — call "
        "`exfer_simulate_transfer` first to confirm the exact fee, and only "
        "call this when the user has explicitly authorised the spend. The "
        "returned `tx_id` can be passed to `exfer_wait_for_tx` to await "
        "confirmation."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "from_address": {
                "type": "string",
                "pattern": "^[0-9a-f]{64}$",
            },
            "to_address": {
                "type": "string",
                "pattern": "^[0-9a-f]{64}$",
            },
            "amount": {
                "type": "integer",
                "minimum": 1,
            },
            "fee": {
                "type": "integer",
                "minimum": 0,
            },
            "datum": {
                "type": "string",
                "description": (
                    "Optional hex (<= 4096 bytes): a generic, app-defined "
                    "on-chain blob attached to the payment (e.g. a receipt or "
                    "reference). Meaning is the application's; read it back "
                    "via exfer_get_transaction (outputs[].datum)."
                ),
                "pattern": "^([0-9a-f]{2})*$",
            },
        },
        "required": ["from_address", "to_address", "amount"],
        "additionalProperties": False,
    },
)


async def transfer(
    client: AsyncClient,
    arguments: dict[str, Any],
    config: Config,
) -> list[mcp_types.TextContent]:
    del config
    kwargs: dict[str, Any] = {
        "from_": arguments["from_address"],
        "to": arguments["to_address"],
        "amount": arguments["amount"],
    }
    if "fee" in arguments:
        kwargs["fee"] = arguments["fee"]
    if "datum" in arguments:
        kwargs["datum"] = arguments["datum"]
    try:
        result = await client.transfer(**kwargs)
    except Exception as exc:
        return render_error(exc)
    return [json_text(result)]
