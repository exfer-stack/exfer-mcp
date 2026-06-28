"""Tests for the read-only pool stats tool (``exfer_earn_pool_stats``).

The pool's HTTP stats API is mocked with respx so we cover the curated field
mapping, the remaining-to-payout math, the pplns/solo type derivation, and the
graceful unreachable / 404 paths — without touching a real pool.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
import respx

from exfer_mcp.tools import HANDLERS, NO_WALLETD_TOOLS, TOOLS
from exfer_mcp.tools.pool_stats import earn_pool_stats

ADDRESS = "e09f6d1af1be5847d51052ae456f964bcc05260f3099fda9a0aa35406d816d1a"
POOL_API = "https://api.ninjaraider.com"


def _account(
    *,
    balance: float = 4_200_000_000,  # 42 EXFER at 1e8 delimiter
    threshold_coins: float = 100,
    pool: str = "exfer-pplns",
    type_: Any = ("pplns",),
    offline: bool = False,
) -> dict[str, Any]:
    return {
        "pool": pool,
        "name": ADDRESS,
        "symbol": "EXFER",
        "amountDelimiter": 100_000_000,
        "balance": balance,
        "balanceUsd": 1.23,
        "type": list(type_) if isinstance(type_, tuple) else type_,
        "paymentThreshold": threshold_coins * 100_000_000,
        "paymentThresholdCoins": threshold_coins,
        "defaultPaymentThresholdCoins": 100,
        "hr": 1234.5,
        "hrWindow": 600,
        "averageHr": 1100.0,
        "averageHrWindow": 21600,
        "offline": offline,
        "lastBeat": 1700000000,
        "workers": [
            {"name": "rig1", "hr": 1000.0, "offline": False},
            {"name": "rig2", "hr": 234.5, "offline": True},
        ],
        "payments": [{"a": 1}, {"a": 2}],
        "rewards": [],
        "dailyProfit": 3.5,
        "dailyProfitUsd": 0.1,
        "settings": {},
        "blockchain": {},
    }


def test_pool_stats_tool_registered_and_read_only() -> None:
    listed = {t.name for t in TOOLS}
    assert "exfer_earn_pool_stats" in listed
    assert "exfer_earn_pool_stats" in HANDLERS
    # Hits the pool API, not walletd → no readiness gate.
    assert "exfer_earn_pool_stats" in NO_WALLETD_TOOLS


@respx.mock
def test_pool_stats_curated_mapping_and_remaining_math() -> None:
    route = respx.get(f"{POOL_API}/exfer-pplns/account/{ADDRESS}").mock(
        return_value=httpx.Response(200, json=_account())
    )
    out = asyncio.run(earn_pool_stats(None, {"address": ADDRESS}, None))  # type: ignore[arg-type]
    assert route.called
    data = json.loads(out[0].text)

    assert data["pool"] == "exfer-pplns"
    assert data["type"] == "pplns"
    assert data["accrued_exfer"] == 42.0
    assert data["payout_threshold_exfer"] == 100.0
    # remaining = 100 - 42
    assert data["remaining_to_payout_exfer"] == 58.0
    assert data["hashrate_hs"] == 1234.5
    assert data["average_hashrate_hs"] == 1100.0
    assert data["online"] is True
    assert data["payments_count"] == 2
    assert data["daily_profit_exfer"] == 3.5
    assert data["pool_api"].endswith(f"/exfer-pplns/account/{ADDRESS}")
    # workers curated to name/hashrate/online
    assert data["workers"] == [
        {"name": "rig1", "hashrate_hs": 1000.0, "online": True},
        {"name": "rig2", "hashrate_hs": 234.5, "online": False},
    ]


@respx.mock
def test_pool_stats_remaining_clamps_at_zero_when_over_threshold() -> None:
    # balance 150 EXFER > 100 threshold → remaining clamps to 0, not negative.
    respx.get(f"{POOL_API}/exfer-pplns/account/{ADDRESS}").mock(
        return_value=httpx.Response(200, json=_account(balance=15_000_000_000))
    )
    out = asyncio.run(earn_pool_stats(None, {"address": ADDRESS}, None))  # type: ignore[arg-type]
    data = json.loads(out[0].text)
    assert data["accrued_exfer"] == 150.0
    assert data["remaining_to_payout_exfer"] == 0.0


@respx.mock
def test_pool_stats_solo_pool_type_and_selection() -> None:
    route = respx.get(f"{POOL_API}/exfer-solo/account/{ADDRESS}").mock(
        return_value=httpx.Response(200, json=_account(pool="exfer-solo", type_=("solo",)))
    )
    out = asyncio.run(
        earn_pool_stats(None, {"address": ADDRESS, "pool": "exfer-solo"}, None)  # type: ignore[arg-type]
    )
    assert route.called
    data = json.loads(out[0].text)
    assert data["pool"] == "exfer-solo"
    assert data["type"] == "solo"


@respx.mock
def test_pool_stats_type_falls_back_to_pool_id_suffix() -> None:
    # No `type` field at all → derive from the pool id suffix.
    acct = _account(pool="exfer-solo", type_=None)
    del acct["type"]
    respx.get(f"{POOL_API}/exfer-solo/account/{ADDRESS}").mock(
        return_value=httpx.Response(200, json=acct)
    )
    out = asyncio.run(
        earn_pool_stats(None, {"address": ADDRESS, "pool": "exfer-solo"}, None)  # type: ignore[arg-type]
    )
    data = json.loads(out[0].text)
    assert data["type"] == "solo"


@respx.mock
def test_pool_stats_404_no_account_yet() -> None:
    respx.get(f"{POOL_API}/exfer-pplns/account/{ADDRESS}").mock(
        return_value=httpx.Response(404, text="not found")
    )
    out = asyncio.run(earn_pool_stats(None, {"address": ADDRESS}, None))  # type: ignore[arg-type]
    msg = out[0].text.lower()
    assert "no account" in msg
    assert "exfer_earn" in out[0].text  # points the agent at how to start


@respx.mock
def test_pool_stats_unreachable_graceful() -> None:
    respx.get(f"{POOL_API}/exfer-pplns/account/{ADDRESS}").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    out = asyncio.run(earn_pool_stats(None, {"address": ADDRESS}, None))  # type: ignore[arg-type]
    msg = out[0].text.lower()
    assert "unreachable" in msg
    assert "exfer_mine_pool_api" in msg


def test_pool_stats_api_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXFER_MINE_POOL_API", "https://pool.example.test/")
    custom = "https://pool.example.test"
    with respx.mock:
        route = respx.get(f"{custom}/exfer-pplns/account/{ADDRESS}").mock(
            return_value=httpx.Response(200, json=_account())
        )
        out = asyncio.run(earn_pool_stats(None, {"address": ADDRESS}, None))  # type: ignore[arg-type]
        assert route.called
    data = json.loads(out[0].text)
    assert data["pool_api"].startswith(custom)
