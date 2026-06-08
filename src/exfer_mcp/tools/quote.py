"""EXFER-QUOTE — issue and verify signed price credentials.

``exfer_quote_issue`` mints a signed quote: a self-contained, off-chain
price credential binding a currency/amount to an exfer amount, signed by
an issuer wallet's key and pinned to the live node's genesis + clock.
Issuing is **Spend-posture** — it uses a managed key to sign a binding
credential — even though it broadcasts nothing.

``exfer_quote_verify`` is the mirror: a pure, key-free Read check that
reconstructs the signing image and confirms authorship, key validity, and
the TTL/expiry windows. It changes no state.
"""

from __future__ import annotations

from typing import Any

import mcp.types as mcp_types
from exfer_walletd import AsyncClient

from ..config import Config
from ._common import json_text, render_error

QUOTE_ISSUE_TOOL = mcp_types.Tool(
    name="exfer_quote_issue",
    description=(
        "Create a SIGNED price credential (an EXFER-QUOTE) using a managed "
        "address's key. This SIGNS WITH A WALLET KEY — it mints a binding "
        "credential another party can present, so treat it as a Spend-posture "
        "action and only call it when the user has authorised issuing the "
        "quote. Binds a `currency`/`amount_minor` price to a fixed "
        "`exfer_amount` payable to `payee_pubkey`, valid for `ttl_secs`. "
        "Returns the signed `quote` object plus a payee-side `htlc_preimage` "
        "(KEEP SECRET — anyone with it can claim the matching HTLC) and its "
        "`htlc_hash_lock`. Pass the returned `quote` to `exfer_quote_verify` "
        "to confirm it before relying on it."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "address": {
                "type": "string",
                "description": "Issuer/signer — must be a managed address (a key walletd holds).",
                "pattern": "^[0-9a-f]{64}$",
            },
            "payee_pubkey": {
                "type": "string",
                "description": "Public key (64 hex) of the party to be paid.",
                "pattern": "^[0-9a-f]{64}$",
            },
            "currency": {
                "type": "string",
                "description": "Pricing-unit code, 3-12 chars [A-Z0-9] (e.g. USD).",
                "pattern": "^[A-Z0-9]{3,12}$",
            },
            "amount_minor": {
                "type": "integer",
                "description": "Price in the currency's minor units (e.g. cents).",
                "minimum": 0,
            },
            "rate_exfers_per_unit": {
                "type": "integer",
                "description": "Conversion rate: exfers per one minor unit of `currency`.",
                "minimum": 0,
            },
            "exfer_amount": {
                "type": "integer",
                "description": ("The only binding amount: exfers payable (1 EXFER = 100_000_000)."),
                "minimum": 1,
            },
            "ttl_secs": {
                "type": "integer",
                "description": "Lifetime in seconds (0 < ttl_secs <= 3600).",
                "minimum": 1,
                "maximum": 3600,
            },
            "payer_pubkey": {
                "type": "string",
                "description": (
                    "Optional: bind the quote to a specific payer (64 hex), making "
                    "it non-transferable. Omit for a bearer quote."
                ),
                "pattern": "^[0-9a-f]{64}$",
            },
            "memo": {
                "type": "string",
                "description": "Optional signed UTF-8 note (<= 256 bytes).",
            },
            "quote_id": {
                "type": "string",
                "description": (
                    "Optional: pin the quote id (32 hex / 16 bytes). Omit to let "
                    "walletd generate random bytes."
                ),
                "pattern": "^[0-9a-f]{32}$",
            },
        },
        "required": [
            "address",
            "payee_pubkey",
            "currency",
            "amount_minor",
            "rate_exfers_per_unit",
            "exfer_amount",
            "ttl_secs",
        ],
        "additionalProperties": False,
    },
)

QUOTE_VERIFY_TOOL = mcp_types.Tool(
    name="exfer_quote_verify",
    description=(
        "Verify a signed EXFER-QUOTE. This is a PURE READ check — no wallet "
        "key is used and NO STATE CHANGES. Reconstructs the signing image "
        "against the live node's genesis + clock and checks the signature, "
        "key validity, and TTL/skew/expiry windows. Returns `{valid, "
        "signer_address, payee_address, genesis_block_id}`; when `valid` is "
        "false a `reason` field explains why. Proves authorship, not "
        "authority — you still decide whether you trust the signer."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "quote": {
                "type": "object",
                "description": (
                    "The signed quote object to verify — the `quote` field "
                    "returned by `exfer_quote_issue`."
                ),
            },
        },
        "required": ["quote"],
        "additionalProperties": False,
    },
)


async def quote_issue(
    client: AsyncClient, arguments: dict[str, Any], config: Config
) -> list[mcp_types.TextContent]:
    del config
    try:
        result = await client.quote_issue(**arguments)
    except Exception as exc:
        return render_error(exc)
    return [json_text(result)]


async def quote_verify(
    client: AsyncClient, arguments: dict[str, Any], config: Config
) -> list[mcp_types.TextContent]:
    del config
    try:
        result = await client.quote_verify(quote=arguments["quote"])
    except Exception as exc:
        return render_error(exc)
    return [json_text(result)]
