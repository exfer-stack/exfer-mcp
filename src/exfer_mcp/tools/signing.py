"""Message signing — prove control of an address without spending.

`exfer_sign_message` signs an arbitrary string with a managed address's
key (domain-separated under EXFER-MSG, so it can never be mistaken for a
transaction). `exfer_verify_message` checks such a signature. Together
they are the off-chain challenge-response primitive for agent identity:
a verifier sends a nonce, the prover signs it, the verifier confirms the
signature and that the key hashes to the claimed address.
"""

from __future__ import annotations

from typing import Any

import mcp.types as mcp_types
from exfer_walletd import AsyncClient

from ..config import Config
from ._common import json_text, render_error

SIGN_MESSAGE_TOOL = mcp_types.Tool(
    name="exfer_sign_message",
    description=(
        "Sign an arbitrary UTF-8 message with a managed address's key, proving "
        "control of that address WITHOUT any on-chain transaction. Returns "
        "`{signature, pubkey, address}`. Use for challenge-response identity "
        "proofs (sign a verifier's nonce). Domain-separated from transaction "
        "signatures, so it can never authorize a payment."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "address": {
                "type": "string",
                "description": "Managed address whose key signs (64 lowercase hex).",
                "pattern": "^[0-9a-f]{64}$",
            },
            "message": {"type": "string", "description": "The message / nonce to sign."},
        },
        "required": ["address", "message"],
        "additionalProperties": False,
    },
)

VERIFY_MESSAGE_TOOL = mcp_types.Tool(
    name="exfer_verify_message",
    description=(
        "Verify an Ed25519 message signature (pure crypto, no wallet needed). "
        "Returns `{valid, address}` where address is what the pubkey hashes to. "
        "Pass the optional `address` to additionally require the key matches a "
        "claimed address — the full check for 'does this counterparty control "
        "address X'."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "pubkey": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "signature": {"type": "string", "pattern": "^[0-9a-f]{128}$"},
            "message": {"type": "string"},
            "address": {
                "type": "string",
                "description": "Optional: require the key to hash to this address.",
                "pattern": "^[0-9a-f]{64}$",
            },
        },
        "required": ["pubkey", "signature", "message"],
        "additionalProperties": False,
    },
)


async def sign_message(
    client: AsyncClient, arguments: dict[str, Any], config: Config
) -> list[mcp_types.TextContent]:
    del config
    try:
        result = await client.sign_message(arguments["address"], arguments["message"])
    except Exception as exc:
        return render_error(exc)
    return [json_text(result)]


async def verify_message(
    client: AsyncClient, arguments: dict[str, Any], config: Config
) -> list[mcp_types.TextContent]:
    del config
    try:
        result = await client.verify_message(
            arguments["pubkey"],
            arguments["signature"],
            arguments["message"],
            address=arguments.get("address"),
        )
    except Exception as exc:
        return render_error(exc)
    return [json_text(result)]
