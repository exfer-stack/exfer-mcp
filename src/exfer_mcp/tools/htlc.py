"""HTLC lifecycle — hash-time-locked contracts for conditional payment.

`lock` funds an output payable to a receiver against a hash_lock,
refundable to the sender after a timeout height. `claim` spends it by
revealing the preimage (receiver, before timeout); `reclaim` refunds the
sender (after timeout). `status` / `list` read the observability index.

This is the atomic pay-for-secret primitive: a payer locks funds to
H(s); the worker can only take the money by revealing s on-chain, so the
payer is guaranteed to learn s the instant the worker is paid. Pair the
claim-watch with `exfer_wait_for_payment` on the HTLC script for
sub-second "claimed" detection.
"""

from __future__ import annotations

from typing import Any

import mcp.types as mcp_types
from exfer import AsyncClient

from ..config import Config
from ._common import json_text, render_error

_HEX64 = {"type": "string", "pattern": "^[0-9a-f]{64}$"}

HTLC_LOCK_TOOL = mcp_types.Tool(
    name="exfer_htlc_lock",
    description=(
        "Lock funds in an HTLC: payable to `receiver`'s pubkey on revealing the "
        "preimage of `hash_lock` before `timeout` (a block height), else "
        "refundable to `from`. SPENDS FUNDS. Returns the lock tx id. The "
        "receiver shares hash_lock = sha256(secret) beforehand."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "from": _HEX64,
            "receiver": {**_HEX64, "description": "Receiver pubkey (64 hex)."},
            "hash_lock": _HEX64,
            "timeout": {"type": "integer", "minimum": 1, "description": "Timeout block height."},
            "amount": {"type": "integer", "minimum": 1},
            "fee": {"type": "integer", "minimum": 0},
            "fee_rate": {"type": "integer", "minimum": 1},
        },
        "required": ["from", "receiver", "hash_lock", "timeout", "amount"],
        "additionalProperties": False,
    },
)

HTLC_CLAIM_TOOL = mcp_types.Tool(
    name="exfer_htlc_claim",
    description=(
        "Claim an HTLC by revealing the `preimage` (receiver action, before "
        "timeout). SPENDS the locked output to you and publishes the preimage "
        "on-chain. Returns the claim tx id."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "from": _HEX64,
            "lock_tx_id": _HEX64,
            "preimage": {"type": "string", "description": "The secret (hex)."},
            "sender": {**_HEX64, "description": "Original sender pubkey (64 hex)."},
            "timeout": {"type": "integer", "minimum": 1},
            "output_index": {"type": "integer", "minimum": 0, "default": 0},
            "fee": {"type": "integer", "minimum": 0},
        },
        "required": ["from", "lock_tx_id", "preimage", "sender", "timeout"],
        "additionalProperties": False,
    },
)

HTLC_RECLAIM_TOOL = mcp_types.Tool(
    name="exfer_htlc_reclaim",
    description=(
        "Reclaim an HTLC refund (sender action, only valid after `timeout`). "
        "SPENDS the locked output back to you. Returns the reclaim tx id."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "from": _HEX64,
            "lock_tx_id": _HEX64,
            "receiver": {**_HEX64, "description": "Receiver pubkey (64 hex)."},
            "hash_lock": _HEX64,
            "timeout": {"type": "integer", "minimum": 1},
            "output_index": {"type": "integer", "minimum": 0, "default": 0},
            "fee": {"type": "integer", "minimum": 0},
        },
        "required": ["from", "lock_tx_id", "receiver", "hash_lock", "timeout"],
        "additionalProperties": False,
    },
)

HTLC_STATUS_TOOL = mcp_types.Tool(
    name="exfer_htlc_status",
    description=(
        "Look up one HTLC's lifecycle (locked / claimed / reclaimed) from the "
        "observability index by lock tx id."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "lock_tx_id": _HEX64,
            "output_index": {"type": "integer", "minimum": 0, "default": 0},
        },
        "required": ["lock_tx_id"],
        "additionalProperties": False,
    },
)

HTLC_LIST_TOOL = mcp_types.Tool(
    name="exfer_htlc_list",
    description=(
        "List tracked HTLCs, filterable by role (sender/receiver/observer), "
        "state (locked/locked_expired/claimed/reclaimed/unknown), height range "
        "and address. Paginated."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "role": {"type": "string", "enum": ["sender", "receiver", "both", "observer"]},
            "state": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ]
            },
            "since_height": {"type": "integer", "minimum": 0},
            "address": _HEX64,
            "limit": {"type": "integer", "minimum": 1},
            "cursor": {"type": "string"},
        },
        "additionalProperties": False,
    },
)


async def htlc_lock(
    client: AsyncClient, arguments: dict[str, Any], config: Config
) -> list[mcp_types.TextContent]:
    del config
    try:
        result = await client.htlc_lock(
            from_=arguments["from"],
            receiver=arguments["receiver"],
            hash_lock=arguments["hash_lock"],
            timeout=arguments["timeout"],
            amount=arguments["amount"],
            fee=arguments.get("fee"),
            fee_rate=arguments.get("fee_rate"),
        )
    except Exception as exc:
        return render_error(exc)
    return [json_text(result)]


async def htlc_claim(
    client: AsyncClient, arguments: dict[str, Any], config: Config
) -> list[mcp_types.TextContent]:
    del config
    try:
        result = await client.htlc_claim(
            from_=arguments["from"],
            lock_tx_id=arguments["lock_tx_id"],
            preimage=arguments["preimage"],
            sender=arguments["sender"],
            timeout=arguments["timeout"],
            output_index=arguments.get("output_index", 0),
            fee=arguments.get("fee"),
        )
    except Exception as exc:
        return render_error(exc)
    return [json_text(result)]


async def htlc_reclaim(
    client: AsyncClient, arguments: dict[str, Any], config: Config
) -> list[mcp_types.TextContent]:
    del config
    try:
        result = await client.htlc_reclaim(
            from_=arguments["from"],
            lock_tx_id=arguments["lock_tx_id"],
            receiver=arguments["receiver"],
            hash_lock=arguments["hash_lock"],
            timeout=arguments["timeout"],
            output_index=arguments.get("output_index", 0),
            fee=arguments.get("fee"),
        )
    except Exception as exc:
        return render_error(exc)
    return [json_text(result)]


async def htlc_status(
    client: AsyncClient, arguments: dict[str, Any], config: Config
) -> list[mcp_types.TextContent]:
    del config
    try:
        result = await client.htlc_status(arguments["lock_tx_id"], arguments.get("output_index", 0))
    except Exception as exc:
        return render_error(exc)
    return [json_text(result)]


async def htlc_list(
    client: AsyncClient, arguments: dict[str, Any], config: Config
) -> list[mcp_types.TextContent]:
    del config
    try:
        result = await client.htlc_list(
            role=arguments.get("role"),
            state=arguments.get("state"),
            since_height=arguments.get("since_height"),
            address=arguments.get("address"),
            limit=arguments.get("limit"),
            cursor=arguments.get("cursor"),
        )
    except Exception as exc:
        return render_error(exc)
    return [json_text(result)]
