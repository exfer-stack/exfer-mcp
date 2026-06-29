"""Cross-chain swap — the agent's on-ramp into EXFER.

walletd (started with ``--swap-pool``) carries its own derived BSC/EVM
address and can atomically swap EXFER <-> USDT/BNB on BSC over HTLC <->
HTLC. That makes USDT — the most liquid, universally available asset — an
on-ramp: an agent funds its BSC address with USDT/BNB (via any exchange or
the operator), then ``swap_get_quote`` + ``swap_execute`` in the
``bnb_to_exfer`` direction turns it into spendable EXFER, entirely from the
agent's own toolset.

Flow:
  1. ``bsc_get_address`` — where to send USDT/BNB (``created:false`` means an
     operator must create the BSC key first; this tool never creates keys).
  2. ``swap_pool_info`` — confirm a pool is configured + see the rate/reserves.
  3. ``swap_get_quote`` (no funds move) — returns a ``swap_id`` + quoted out.
  4. ``swap_execute`` — MOVES FUNDS (locks the input leg).
  5. ``swap_status`` — poll until terminal; ``swap_refund`` recovers a stall.

SAFETY: ``swap_execute`` moves real cross-chain value with no per-call human
approval — bound the blast radius with a small float. The ``exfer_to_bnb``
(off-ramp) direction is refused by walletd while spend caps are configured,
because its EXFER leg is not yet metered by the allowance ledger;
``bnb_to_exfer`` (on-ramp) is unaffected.
"""

from __future__ import annotations

from typing import Any

import mcp.types as mcp_types
from exfer import AsyncClient

from ..config import Config
from ._common import json_text, render_error

_HEX64 = {"type": "string", "pattern": "^[0-9a-f]{64}$"}

# The swap lifecycle, so an agent knows what to poll for and when it is done.
# Terminal states: "completed" (success), "refunded"/"failed" (recovered/aborted).
_STATUS_NOTE = (
    "Status lifecycle: quoted -> user_locked -> pool_locked -> claiming -> "
    "completed. TERMINAL states are 'completed' (success), 'refunded', and "
    "'failed'; keep polling on any other value. Intermediate states can be "
    "skipped between polls (cross-chain settlement is fast). In the v2 "
    "(pool-locks-first) flow the local status goes quoted -> user_locked -> "
    "completed (the pool settles both legs), so don't wait for pool_locked/"
    "claiming there."
)

BSC_GET_ADDRESS_TOOL = mcp_types.Tool(
    name="exfer_bsc_get_address",
    description=(
        "Return this wallet's BSC/EVM address (EIP-55) to fund with USDT/BNB "
        "for on-ramping into EXFER, or `created:false` if no BSC key exists "
        "yet (an operator must create one — this tool never creates keys). "
        "Read-only."
    ),
    inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
)

BSC_GET_BALANCE_TOOL = mcp_types.Tool(
    name="exfer_bsc_get_balance",
    description=(
        "Native BNB balance of this wallet's BSC address, as decimal-string wei "
        "(18 decimals; 1 BNB = 1e18 wei). Returns `bnb_wei` and `gas_reserve_wei` "
        "(wei to hold back for the BSC tx's own gas; spendable = bnb_wei - "
        "gas_reserve_wei). Use this to confirm a BNB/USDT deposit landed BEFORE "
        "quoting an on-ramp, and to verify the BNB you received after an "
        "exfer_to_bnb swap (the swap tools settle the BNB leg but do not echo "
        "your BSC balance). Read-only."
    ),
    inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
)

SWAP_POOL_INFO_TOOL = mcp_types.Tool(
    name="exfer_swap_pool_info",
    description=(
        "Swap pool configuration + current rate/reserves. Use it to confirm a "
        "pool is available (walletd started with --swap-pool) and to check how "
        "much EXFER you can get before quoting. Read-only."
    ),
    inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
)

SWAP_GET_QUOTE_TOOL = mcp_types.Tool(
    name="exfer_swap_get_quote",
    description=(
        "Quote a cross-chain swap — NO funds move. `direction` is "
        "'bnb_to_exfer' (on-ramp: receive EXFER) or 'exfer_to_bnb' (off-ramp: "
        "spend EXFER, receive BNB). `amount_in` is a decimal string in the INPUT "
        "asset's units: BNB for bnb_to_exfer, EXFER for exfer_to_bnb. `from` is "
        "always the EXFER-side address regardless of direction — the address "
        "that RECEIVES EXFER (bnb_to_exfer) or FUNDS the EXFER it spends "
        "(exfer_to_bnb); it is NOT the BNB source (the BNB leg always uses this "
        "wallet's own derived BSC address). Returns a swap record: `swap_id` "
        "(pass to exfer_swap_execute before `expires_at`), quoted `amount_out` "
        "(+ `amount_out_units` in smallest units), `fee_bps` (pool fee), and "
        "`network_fee_bnb` (the BSC gas estimate for the on-chain leg, "
        "informational — paid from your BSC balance, not added to amount_in). "
        "Some fields are direction-specific and may be null: bnb_to_exfer "
        "populates `pool_bsc_address` + `bsc_timeout_sec`; exfer_to_bnb "
        "populates `pool_exfer_pubkey` + `exfer_timeout_height`."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "direction": {"type": "string", "enum": ["bnb_to_exfer", "exfer_to_bnb"]},
            "amount_in": {"type": "string", "description": "Decimal amount in the input asset."},
            "from": {**_HEX64, "description": "EXFER address for the EXFER leg (64 hex)."},
            "flow": {
                "type": "string",
                "enum": ["v1", "v2"],
                "description": (
                    "Optional protocol flow. 'v2' (pool-locks-first; the default — "
                    "the user locks once and walks away, the pool settles both legs "
                    "and a buy is much faster) or 'v1' (legacy user-locks-first). "
                    "Omit to use v2."
                ),
            },
        },
        "required": ["direction", "amount_in", "from"],
        "additionalProperties": False,
    },
)

SWAP_EXECUTE_TOOL = mcp_types.Tool(
    name="exfer_swap_execute",
    description=(
        "Execute a quoted swap by `swap_id`. MOVES FUNDS — locks the input leg "
        "of the cross-chain atomic swap. Then poll exfer_swap_status until the "
        "status is terminal. No per-call human approval; bound your float. " + _STATUS_NOTE
    ),
    inputSchema={
        "type": "object",
        "properties": {"swap_id": {"type": "string"}},
        "required": ["swap_id"],
        "additionalProperties": False,
    },
)

SWAP_STATUS_TOOL = mcp_types.Tool(
    name="exfer_swap_status",
    description=(
        "Look up one swap's current lifecycle state by `swap_id`. Read-only. "
        "Poll this after exfer_swap_execute. " + _STATUS_NOTE
    ),
    inputSchema={
        "type": "object",
        "properties": {"swap_id": {"type": "string"}},
        "required": ["swap_id"],
        "additionalProperties": False,
    },
)

SWAP_REFUND_TOOL = mcp_types.Tool(
    name="exfer_swap_refund",
    description=(
        "Refund a stalled or expired swap by `swap_id` (recovery path). SPENDS "
        "a transaction to reclaim the locked input leg."
    ),
    inputSchema={
        "type": "object",
        "properties": {"swap_id": {"type": "string"}},
        "required": ["swap_id"],
        "additionalProperties": False,
    },
)

SWAP_LIST_TOOL = mcp_types.Tool(
    name="exfer_swap_list",
    description="List swaps this wallet has initiated (the local journal). Read-only.",
    inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
)


async def bsc_get_address(
    client: AsyncClient, arguments: dict[str, Any], config: Config
) -> list[mcp_types.TextContent]:
    del arguments, config
    try:
        result = await client.bsc_get_address()
    except Exception as exc:
        return render_error(exc)
    return [json_text(result)]


async def bsc_get_balance(
    client: AsyncClient, arguments: dict[str, Any], config: Config
) -> list[mcp_types.TextContent]:
    del arguments, config
    try:
        result = await client.bsc_get_balances()
    except Exception as exc:
        return render_error(exc)
    return [json_text(result)]


async def swap_pool_info(
    client: AsyncClient, arguments: dict[str, Any], config: Config
) -> list[mcp_types.TextContent]:
    del arguments, config
    try:
        result = await client.swap_pool_info()
    except Exception as exc:
        return render_error(exc)
    return [json_text(result)]


async def swap_get_quote(
    client: AsyncClient, arguments: dict[str, Any], config: Config
) -> list[mcp_types.TextContent]:
    del config
    try:
        result = await client.swap_get_quote(
            direction=arguments["direction"],
            amount_in=arguments["amount_in"],
            from_=arguments["from"],
            # Default to v2 (pool-locks-first) when the caller omits `flow`:
            # the user locks once and walks away, the pool settles both legs,
            # and a buy is much faster. walletd safely auto-downgrades a v2
            # request to v1 if the pool isn't v2-capable, so this is safe even
            # against an old pool. An explicit "v1"/"v2" is still honoured.
            flow=arguments.get("flow") or "v2",
        )
    except Exception as exc:
        return render_error(exc)
    return [json_text(result)]


async def swap_execute(
    client: AsyncClient, arguments: dict[str, Any], config: Config
) -> list[mcp_types.TextContent]:
    del config
    try:
        result = await client.swap_execute(arguments["swap_id"])
    except Exception as exc:
        return render_error(exc)
    return [json_text(result)]


async def swap_status(
    client: AsyncClient, arguments: dict[str, Any], config: Config
) -> list[mcp_types.TextContent]:
    del config
    try:
        result = await client.swap_status(arguments["swap_id"])
    except Exception as exc:
        return render_error(exc)
    return [json_text(result)]


async def swap_refund(
    client: AsyncClient, arguments: dict[str, Any], config: Config
) -> list[mcp_types.TextContent]:
    del config
    try:
        result = await client.swap_refund(arguments["swap_id"])
    except Exception as exc:
        return render_error(exc)
    return [json_text(result)]


async def swap_list(
    client: AsyncClient, arguments: dict[str, Any], config: Config
) -> list[mcp_types.TextContent]:
    del arguments, config
    try:
        result = await client.swap_list()
    except Exception as exc:
        return render_error(exc)
    return [json_text(result)]
