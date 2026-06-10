"""Honor read-tools — confirm and reverse-look-up on-chain quote settlements.

These are the read side of the EXFER-QUOTE honor flow. After a quote is
settled on-chain (a transfer carrying the 16-byte ``quote_id`` as an
inline datum), an agent uses these to *prove* the settlement honored the
quote:

- :func:`get_output_datum` reads a settlement output back and confirms it
  carries your ``quote_id`` (the honor *verify* step).
- :func:`find_settlements_by_quote_id` is the reverse lookup: given a
  ``quote_id``, find the settlement outpoint(s) that paid it (the honor
  gate's *first* step).

Both are indexer-backed, so they require walletd to be started with an
indexer (``--indexer-rpc``); without one they return an
``IndexerNotConfigured`` error.
"""

from __future__ import annotations

from typing import Any

import mcp.types as mcp_types
from exfer import AsyncClient

from ..config import Config
from ._common import json_text, render_error

GET_OUTPUT_DATUM_TOOL = mcp_types.Tool(
    name="exfer_get_output_datum",
    description=(
        "Read the datum a single transaction output carries — the honor "
        "VERIFY step. After settling a quote on-chain, read the settlement "
        "output back to confirm it carries your quote_id. Returns JSON "
        "{quote_id, unhonorable}: a non-null quote_id (32 hex) means a strict "
        "16-byte inline datum was indexed and the settlement is "
        "honor-verifiable; unhonorable is true for a datum_hash-only output "
        "(bytes never published inline, so the quote_id can't be confirmed); "
        "an output with no datum returns {quote_id: null, unhonorable: false}. "
        "Requires an indexer-backed walletd."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "tx_id": {
                "type": "string",
                "description": "Settlement transaction id, 64 lowercase hex.",
                "pattern": "^[0-9a-f]{64}$",
            },
            "output_index": {
                "type": "integer",
                "description": "Index of the output within the transaction.",
                "minimum": 0,
            },
        },
        "required": ["tx_id", "output_index"],
        "additionalProperties": False,
    },
)

FIND_SETTLEMENTS_BY_QUOTE_ID_TOOL = mcp_types.Tool(
    name="exfer_find_settlements_by_quote_id",
    description=(
        "Reverse-lookup: given a quote_id, find the on-chain settlement "
        "outpoint(s) that paid it — the honor gate's FIRST step. Returns JSON "
        "{settlements: [{tx_id, output_index}, ...]}: an empty array when "
        "nothing on-chain references the quote yet, and possibly more than one "
        "entry when it was settled multiple times. Pair each outpoint with "
        "exfer_get_output_datum to verify the inline quote_id read-back. "
        "Requires an indexer-backed walletd."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "quote_id": {
                "type": "string",
                "description": "Quote id, 16 bytes as 32 lowercase hex.",
                "pattern": "^[0-9a-f]{32}$",
            },
        },
        "required": ["quote_id"],
        "additionalProperties": False,
    },
)


async def get_output_datum(
    client: AsyncClient, arguments: dict[str, Any], config: Config
) -> list[mcp_types.TextContent]:
    del config
    try:
        result = await client.get_output_datum(arguments["tx_id"], arguments["output_index"])
    except Exception as exc:
        return render_error(exc)
    return [json_text(result)]


async def find_settlements_by_quote_id(
    client: AsyncClient, arguments: dict[str, Any], config: Config
) -> list[mcp_types.TextContent]:
    del config
    try:
        result = await client.find_settlements_by_quote_id(arguments["quote_id"])
    except Exception as exc:
        return render_error(exc)
    return [json_text(result)]
