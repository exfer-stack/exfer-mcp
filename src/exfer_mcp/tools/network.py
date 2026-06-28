"""Network + explorer tools — read-only views of the Exfer L1 node itself.

Unlike every other tool in this server, these do NOT go through walletd: they
query the node's JSON-RPC surface directly (:mod:`exfer_mcp.node_fetch`), so the
agent ships with explorer-grade chain data — whole-network status, an estimated
network hashrate, and lookup of ANY block / transaction (not just the wallet's
own). All four are strictly read-only (no keys, no spend), so the server lists
them in ``NO_WALLETD_TOOLS`` and dispatches them without the managed-walletd
readiness gate — they work even while walletd is still booting or down.

  exfer_network_status     -> node get_node_info (height, peers, mempool, …)
  exfer_network_hashrate   -> estimated whole-network hashrate (derived, honest)
  exfer_get_block          -> explorer block lookup by height or hash
  exfer_get_transaction    -> explorer tx lookup by hash (mempool or confirmed)

Hashrate derivation (see node consensus/{pow,difficulty}.rs):

* PoW acceptance is ``argon2id(header) < difficulty_target`` (256-bit
  big-endian), so the expected number of Argon2id hashes to find one block is
  ``2^256 / difficulty_target`` — this is exactly the node's
  ``work_from_target`` (floor(2^256 / target)).
* The node targets one block every ``TARGET_BLOCK_TIME_SECS`` = 10s.
* Over a recent window we sum each block's expected work and divide by the
  ACTUAL wall-clock span (tip timestamp minus window-start timestamp). That makes
  the estimate self-correcting: if the window ran fast/slow the observed span
  reflects it, instead of assuming a perfect 10s cadence. The unit is Argon2id
  hashes/sec (memory-hard; not comparable to a SHA-256 ASIC's H/s).
"""

from __future__ import annotations

from typing import Any

import mcp.types as mcp_types
from exfer import AsyncClient

from ..config import Config
from ..node_fetch import NodeRpcError, NodeUnreachableError, node_rpc, node_rpc_endpoints
from ._common import json_text, text

# Mirrors node src/types/mod.rs TARGET_BLOCK_TIME_SECS — the consensus block
# cadence the DAA retargets toward. Used only as a fallback denominator when a
# window's observed timespan is non-positive (e.g. clock skew, single block).
TARGET_BLOCK_TIME_SECS = 10

# Default look-back window for the hashrate estimate. Kept modest so the two
# bounding get_block calls stay cheap; overridable via the tool argument.
_DEFAULT_WINDOW_BLOCKS = 60
_MAX_WINDOW_BLOCKS = 2016

_2_256 = 1 << 256


def _node_err(exc: Exception) -> list[mcp_types.TextContent]:
    """Render a node-side failure as agent-readable text (mirrors render_error)."""
    if isinstance(exc, NodeUnreachableError):
        return [
            text(
                f"the Exfer node looks unreachable from here ({exc}). Set EXFER_NODE_RPC "
                f"to a node you can reach (or run one locally), then retry."
            )
        ]
    if isinstance(exc, NodeRpcError):
        return [text(f"node error (code {exc.code}): {exc.message}")]
    return [text(f"{type(exc).__name__}: {exc}")]


def _work_from_target(target_hex: str) -> int:
    """Expected hashes to find a block at this target: floor(2^256 / target).

    ``target_hex`` is the 64-char big-endian ``difficulty_target`` from the
    node. A zero target means no hash can satisfy it → infinite work; we clamp
    to the same 2^256-1 saturation the node uses, but in practice the target is
    never zero on a live chain.
    """
    target = int(target_hex, 16)
    if target <= 0:
        return _2_256 - 1
    return _2_256 // target


# ---------------------------------------------------------------------------
# exfer_network_status
# ---------------------------------------------------------------------------

NETWORK_STATUS_TOOL = mcp_types.Tool(
    name="exfer_network_status",
    description=(
        "Live status of the whole Exfer network as seen by the upstream node "
        "(NOT the wallet). Returns the node's get_node_info JSON: version, "
        "network, genesis_block_id, tip_height, tip_block_id, tip_age_seconds, "
        "peer_count, mempool_size, mempool_bytes, uptime_seconds, and metrics. "
        "Read-only. Use this for 'is the network healthy / what height is it'."
    ),
    inputSchema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
)


async def network_status(
    client: AsyncClient,
    arguments: dict[str, Any],
    config: Config,
) -> list[mcp_types.TextContent]:
    del client, arguments, config
    try:
        info = await node_rpc("get_node_info")
    except Exception as exc:
        return _node_err(exc)
    return [json_text(info)]


# ---------------------------------------------------------------------------
# exfer_network_hashrate
# ---------------------------------------------------------------------------

NETWORK_HASHRATE_TOOL = mcp_types.Tool(
    name="exfer_network_hashrate",
    description=(
        "Estimate the whole-network hashrate (Argon2id hashes/sec). This is a "
        "DERIVED ESTIMATE, not a reported counter: it sums each recent block's "
        "expected work (2^256 / difficulty_target) over a look-back window and "
        "divides by the window's actual wall-clock span. Returns {difficulty, "
        "est_hashrate_hs, work_per_block, target_block_seconds, window_blocks, "
        "window_seconds, tip_height, is_estimate}. The unit is memory-hard "
        "Argon2id H/s and is NOT comparable to a SHA-256 ASIC's hashrate. "
        "Read-only."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "window_blocks": {
                "type": "integer",
                "description": (
                    "How many blocks back to average over (default 60). Larger = "
                    "smoother but slower-reacting. Clamped to [2, 2016]."
                ),
                "minimum": 2,
                "maximum": _MAX_WINDOW_BLOCKS,
            }
        },
        "additionalProperties": False,
    },
)


async def network_hashrate(
    client: AsyncClient,
    arguments: dict[str, Any],
    config: Config,
) -> list[mcp_types.TextContent]:
    del client, config
    window = int(arguments.get("window_blocks") or _DEFAULT_WINDOW_BLOCKS)
    window = max(2, min(window, _MAX_WINDOW_BLOCKS))
    try:
        info = await node_rpc("get_node_info")
        tip_height = int(info["tip_height"]) if isinstance(info, dict) else 0
        # Don't reach below genesis: shrink the window to what the chain has.
        window = min(window, tip_height)
        if window < 1:
            # Genesis-only chain: no interval to measure. Fall back to the
            # single tip block's work over the target cadence, flagged honestly.
            tip_block = await node_rpc("get_block", {"height": tip_height})
            work = _work_from_target(tip_block["difficulty_target"])  # type: ignore[index]
            return [
                json_text(
                    {
                        "difficulty": tip_block["difficulty_target"],  # type: ignore[index]
                        "work_per_block": work,
                        "est_hashrate_hs": work / TARGET_BLOCK_TIME_SECS,
                        "target_block_seconds": TARGET_BLOCK_TIME_SECS,
                        "window_blocks": 0,
                        "window_seconds": 0,
                        "tip_height": tip_height,
                        "is_estimate": True,
                        "note": (
                            "genesis-only / single-block chain: no observed interval, "
                            "assuming the target block time"
                        ),
                    }
                )
            ]

        start_height = tip_height - window
        tip_block = await node_rpc("get_block", {"height": tip_height})
        start_block = await node_rpc("get_block", {"height": start_height})
    except Exception as exc:
        return _node_err(exc)

    if not (isinstance(tip_block, dict) and isinstance(start_block, dict)):
        return [text("node returned an unexpected get_block shape")]

    tip_ts = int(tip_block["timestamp"])
    start_ts = int(start_block["timestamp"])
    window_seconds = tip_ts - start_ts

    # Expected work per block from the tip's current difficulty (the rate the
    # network is solving at NOW). difficulty_target is near-constant within a
    # retarget window, so the tip target is the right "current" figure.
    work_per_block = _work_from_target(tip_block["difficulty_target"])

    # est H/s = total expected work over the window / observed span. If the
    # observed span is non-positive (clock skew / equal timestamps), fall back
    # to the target cadence so we still return a finite, honest number.
    if window_seconds > 0:
        est_hashrate = (work_per_block * window) / window_seconds
    else:
        window_seconds = window * TARGET_BLOCK_TIME_SECS
        est_hashrate = work_per_block / TARGET_BLOCK_TIME_SECS

    return [
        json_text(
            {
                "difficulty": tip_block["difficulty_target"],
                "work_per_block": work_per_block,
                "est_hashrate_hs": est_hashrate,
                "target_block_seconds": TARGET_BLOCK_TIME_SECS,
                "window_blocks": window,
                "window_seconds": window_seconds,
                "tip_height": tip_height,
                "is_estimate": True,
            }
        )
    ]


# ---------------------------------------------------------------------------
# exfer_get_block
# ---------------------------------------------------------------------------

GET_BLOCK_TOOL = mcp_types.Tool(
    name="exfer_get_block",
    description=(
        "Explorer-grade block lookup from the node by height OR block hash "
        "(provide exactly one). Returns the block header JSON: {hash, height, "
        "timestamp, tx_count, transactions (tx_id list), prev_block_id, "
        "difficulty_target, nonce, state_root, tx_root}. Read-only — works for "
        "ANY block, not just the wallet's."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "height": {
                "type": "integer",
                "description": "Block height to fetch.",
                "minimum": 0,
            },
            "block_id": {
                "type": "string",
                "description": "Block hash as 64 lowercase hex characters.",
                "pattern": "^[0-9a-f]{64}$",
            },
        },
        "additionalProperties": False,
    },
)


async def get_block(
    client: AsyncClient,
    arguments: dict[str, Any],
    config: Config,
) -> list[mcp_types.TextContent]:
    del client, config
    block_id = arguments.get("block_id")
    height = arguments.get("height")
    if block_id is not None and height is not None:
        return [text("provide exactly one of 'height' or 'block_id', not both")]
    # Node's get_block params: {"hash": <hex>} or {"height": <u64>}.
    if block_id is not None:
        params: dict[str, Any] = {"hash": block_id}
    elif height is not None:
        params = {"height": int(height)}
    else:
        return [text("provide one of 'height' or 'block_id'")]
    try:
        block = await node_rpc("get_block", params)
    except Exception as exc:
        return _node_err(exc)
    return [json_text(block)]


# ---------------------------------------------------------------------------
# exfer_get_transaction
# ---------------------------------------------------------------------------

GET_TRANSACTION_TOOL = mcp_types.Tool(
    name="exfer_get_transaction",
    description=(
        "Explorer-grade transaction lookup from the node by tx_id. Searches the "
        "mempool first, then the confirmed chain. Returns {tx_id, tx_hex, "
        "in_mempool, and (if confirmed) block_hash + block_height}. Read-only — "
        "works for ANY transaction, not just the wallet's."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "tx_id": {
                "type": "string",
                "description": "Transaction id as 64 lowercase hex characters.",
                "pattern": "^[0-9a-f]{64}$",
            }
        },
        "required": ["tx_id"],
        "additionalProperties": False,
    },
)


async def get_transaction(
    client: AsyncClient,
    arguments: dict[str, Any],
    config: Config,
) -> list[mcp_types.TextContent]:
    del client, config
    # Node's get_transaction param key is "hash".
    try:
        tx = await node_rpc("get_transaction", {"hash": arguments["tx_id"]})
    except Exception as exc:
        return _node_err(exc)
    return [json_text(tx)]


# Re-exported so a smoke test / introspection can show the endpoints in use.
__all__ = [
    "GET_BLOCK_TOOL",
    "GET_TRANSACTION_TOOL",
    "NETWORK_HASHRATE_TOOL",
    "NETWORK_STATUS_TOOL",
    "get_block",
    "get_transaction",
    "network_hashrate",
    "network_status",
    "node_rpc_endpoints",
]
