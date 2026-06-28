"""Pool stats — read your mining pool's view of your account.

The :mod:`exfer_mcp.tools.earn` tools mine to a public EXFER stratum pool with
the agent's own address as the payout. The pool, not the chain, tracks the
agent's *pending* (accrued, not-yet-paid) balance, applies a payout threshold
("how much before it pays out"), and reports the agent's pool-side hashrate.
That view lives on the pool's HTTP stats API, which is separate from both
walletd and the L1 node — so this tool talks to it directly with its own tiny
httpx client, in the same read-only, error-hint style as
:mod:`exfer_mcp.node_fetch`.

The default pool (``ssl://ninjaraider.com:44913`` in :mod:`earn`) runs a
MiningCore-like API on a separate ``api.`` host. We pair the two by baking
``https://api.ninjaraider.com`` as the default stats base, overridable via
``EXFER_MINE_POOL_API``. The pool id (``exfer-pplns`` / ``exfer-solo``) selects
the PPLNS or the SOLO pool; it defaults to ``EXFER_MINE_POOL_ID`` or
``exfer-pplns``.

  exfer_earn_pool_stats(address[, pool])
    -> {accrued, payout_threshold, remaining_to_payout, hashrate, workers, …}

Strictly read-only (no keys, no spend); needs no walletd, so the server lists it
in ``NO_WALLETD_TOOLS`` and dispatches it without the managed-walletd readiness
gate.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import mcp.types as mcp_types
from exfer import AsyncClient

from ..config import Config
from ._common import json_text, text

# The default pool's stats API host, paired with the default stratum pool in
# :mod:`earn` (``ssl://ninjaraider.com:44913``). Override with EXFER_MINE_POOL_API.
DEFAULT_POOL_API = os.environ.get("EXFER_MINE_POOL_API", "https://api.ninjaraider.com")

# Which pool id to query when the caller doesn't pass one. The default pool runs
# two EXFER pools: "exfer-pplns" (shared PPLNS) and "exfer-solo" (solo).
DEFAULT_POOL_ID = os.environ.get("EXFER_MINE_POOL_ID", "exfer-pplns")

# Pool API divides on-chain amounts by this to get whole EXFER. The API echoes
# its own `amountDelimiter` (1e8); we use that when present and fall back to 1e8.
_DEFAULT_AMOUNT_DELIMITER = 100_000_000

# Short read-only timeout. Honours EXFER_MCP_HTTPX_TIMEOUT like the node client,
# but with a tighter default so an unreachable pool fails fast.
_DEFAULT_TIMEOUT = 15.0


def pool_api_base() -> str:
    """The pool stats API base URL, trailing slash stripped."""
    return (os.environ.get("EXFER_MINE_POOL_API") or DEFAULT_POOL_API).strip().rstrip("/")


def _timeout() -> float:
    val = os.environ.get("EXFER_MCP_HTTPX_TIMEOUT")
    if not val:
        return _DEFAULT_TIMEOUT
    try:
        parsed = float(val)
    except ValueError:
        return _DEFAULT_TIMEOUT
    return parsed if parsed > 0 else _DEFAULT_TIMEOUT


def _as_float(value: Any) -> float:
    """Coerce a maybe-stringy numeric pool field to float, defaulting to 0.0."""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _pool_type(account: dict[str, Any], pool_id: str) -> str | None:
    """Derive pplns/solo from the account's `type` (list or str) or the id.

    The /pools listing reports ``type: ["pplns"]`` (or ``["solo"]``); the
    per-account payload sometimes echoes it. Fall back to a suffix match on the
    pool id (``exfer-solo`` / ``exfer-pplns``) so the agent always gets a label.
    """
    raw = account.get("type")
    if isinstance(raw, list) and raw:
        return str(raw[0]).lower()
    if isinstance(raw, str) and raw:
        return raw.lower()
    low = pool_id.lower()
    if low.endswith("solo"):
        return "solo"
    if low.endswith("pplns"):
        return "pplns"
    return None


def _curate(account: dict[str, Any], pool_id: str, url: str) -> dict[str, Any]:
    """Map the raw MiningCore-like account payload to a clean curated dict."""
    delimiter = _as_float(account.get("amountDelimiter")) or _DEFAULT_AMOUNT_DELIMITER
    accrued = _as_float(account.get("balance")) / delimiter
    # paymentThresholdCoins is the threshold already in whole EXFER (e.g. 100).
    threshold = _as_float(account.get("paymentThresholdCoins"))
    remaining = max(0.0, threshold - accrued)

    workers_raw = account.get("workers")
    workers: list[dict[str, Any]] = []
    if isinstance(workers_raw, list):
        for w in workers_raw:
            if isinstance(w, dict):
                workers.append(
                    {
                        "name": w.get("name"),
                        "hashrate_hs": _as_float(w.get("hr")),
                        "online": not bool(w.get("offline", False)),
                    }
                )

    payments = account.get("payments")
    payments_count = len(payments) if isinstance(payments, list) else 0

    return {
        "pool": account.get("pool") or pool_id,
        "type": _pool_type(account, pool_id),
        "accrued_exfer": accrued,
        "payout_threshold_exfer": threshold,
        "remaining_to_payout_exfer": remaining,
        "hashrate_hs": _as_float(account.get("hr")),
        "average_hashrate_hs": _as_float(account.get("averageHr")),
        "online": not bool(account.get("offline", False)),
        "workers": workers,
        "payments_count": payments_count,
        "daily_profit_exfer": _as_float(account.get("dailyProfit")),
        "pool_api": url,
    }


POOL_STATS_TOOL = mcp_types.Tool(
    name="exfer_earn_pool_stats",
    description=(
        "Read YOUR mining pool's view of your account: pool-side ACCRUED (unpaid) "
        "balance in EXFER, the pool's PAYOUT THRESHOLD (how much before it pays "
        "out), how much is left to reach it, your pool hashrate, and your workers. "
        "This is the pool's pending balance, NOT your on-chain wallet balance (use "
        "exfer_get_balance for that). Works for both the shared PPLNS pool "
        "('exfer-pplns') and the SOLO pool ('exfer-solo') — pass `pool` to pick. "
        "Read-only; queries the pool's stats API directly (not walletd)."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "address": {
                "type": "string",
                "description": "Your payout / miner address (64 lowercase hex) — the one you mine to.",
                "pattern": "^[0-9a-f]{64}$",
            },
            "pool": {
                "type": "string",
                "description": (
                    "Pool id on the stats API. 'exfer-pplns' (shared) or "
                    f"'exfer-solo' (solo). Default: {DEFAULT_POOL_ID}."
                ),
            },
        },
        "required": ["address"],
        "additionalProperties": False,
    },
)


async def earn_pool_stats(
    client: AsyncClient,
    arguments: dict[str, Any],
    config: Config,
) -> list[mcp_types.TextContent]:
    del client, config  # pool stats are independent of walletd
    address = arguments["address"]
    pool_id = arguments.get("pool") or DEFAULT_POOL_ID
    base = pool_api_base()
    url = f"{base}/{pool_id}/account/{address}"
    try:
        async with httpx.AsyncClient(timeout=_timeout()) as http:
            resp = await http.get(url)
            resp.raise_for_status()
            account = resp.json()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 404:
            return [
                text(
                    f"pool '{pool_id}' has no account for this address yet (the pool "
                    f"credits you once it accepts your first share — start mining with "
                    f"exfer_earn, then retry). pool API: {url}"
                )
            ]
        return [
            text(
                f"pool stats API returned HTTP {status} for '{pool_id}' — check the "
                f"pool id (try 'exfer-pplns' or 'exfer-solo') and EXFER_MINE_POOL_API. "
                f"pool API: {url}"
            )
        ]
    except (httpx.HTTPError, ValueError) as exc:
        return [
            text(
                f"the pool stats API looks unreachable or returned no usable data "
                f"({type(exc).__name__}: {exc}). Set EXFER_MINE_POOL_API to a pool "
                f"whose stats host you can reach, then retry. pool API: {base}"
            )
        ]
    if not isinstance(account, dict):
        return [text(f"pool stats API returned an unexpected (non-object) shape. pool API: {url}")]
    return [json_text(_curate(account, pool_id, url))]


__all__ = [
    "POOL_STATS_TOOL",
    "earn_pool_stats",
    "pool_api_base",
]
