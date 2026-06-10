"""``payment_uri_encode`` + ``payment_uri_decode`` — share payment requests.

BIP21-style ``exfer:`` URIs let an agent hand a counterparty a single
string that encodes who to pay, how much, and any memo. Both methods
are pure on the walletd side — no I/O, no key access.
"""

from __future__ import annotations

from typing import Any

import mcp.types as mcp_types
from exfer import AsyncClient

from ..config import Config
from ._common import json_text, render_error, text

PAYMENT_URI_ENCODE_TOOL = mcp_types.Tool(
    name="exfer_payment_uri_encode",
    description=(
        "Build a BIP21-style `exfer:<address>?amount=...&memo=...` URI. Useful "
        "when the agent needs to share a payment request the counterparty can "
        "click or hand to their own wallet."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "address": {
                "type": "string",
                "description": "Recipient address.",
                "pattern": "^[0-9a-f]{64}$",
            },
            "amount": {
                "type": "integer",
                "description": "Optional amount in exfers.",
                "minimum": 1,
            },
            "memo": {
                "type": "string",
                "description": "Optional human-readable note.",
                "maxLength": 256,
            },
            "label": {
                "type": "string",
                "description": "Optional wallet-side label.",
                "maxLength": 64,
            },
        },
        "required": ["address"],
        "additionalProperties": False,
    },
)


async def payment_uri_encode(
    client: AsyncClient,
    arguments: dict[str, Any],
    config: Config,
) -> list[mcp_types.TextContent]:
    del config
    kwargs: dict[str, Any] = {"address": arguments["address"]}
    for k in ("amount", "memo", "label"):
        if k in arguments:
            kwargs[k] = arguments[k]
    try:
        uri = await client.payment_uri_encode(**kwargs)
    except Exception as exc:
        return render_error(exc)
    return [text(uri)]


PAYMENT_URI_DECODE_TOOL = mcp_types.Tool(
    name="exfer_payment_uri_decode",
    description=(
        "Parse a BIP21-style `exfer:` URI back into its component fields "
        "(address, amount, memo, label, …). Useful when the agent receives a "
        "payment request from a counterparty."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "uri": {
                "type": "string",
                "description": "An `exfer:<address>[?...]` URI.",
                "pattern": "^exfer:",
            }
        },
        "required": ["uri"],
        "additionalProperties": False,
    },
)


async def payment_uri_decode(
    client: AsyncClient,
    arguments: dict[str, Any],
    config: Config,
) -> list[mcp_types.TextContent]:
    del config
    try:
        result = await client.payment_uri_decode(arguments["uri"])
    except Exception as exc:
        return render_error(exc)
    return [json_text(result)]
