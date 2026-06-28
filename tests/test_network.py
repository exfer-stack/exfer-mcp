"""Tests for the read-only network / explorer tools and the node JSON-RPC
client they sit on (``exfer_mcp.node_fetch`` + ``exfer_mcp.tools.network``).

The node is mocked two ways:

* the tool handlers patch ``node_rpc`` directly (fast, no HTTP) so we assert the
  param mapping + the hashrate math in isolation;
* the node client itself is exercised against a respx-mocked HTTP node so the
  JSON-RPC envelope / failover / error mapping is covered end to end.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from exfer_mcp import node_fetch
from exfer_mcp.node_fetch import NodeRpcError, NodeUnreachableError
from exfer_mcp.tools import HANDLERS, NO_WALLETD_TOOLS, TOOLS
from exfer_mcp.tools import network as net

TX_ID = "a02ab025d75a295540d681f89da3f8bfed894e02cea721085facbf9ad4525c68"
BLOCK_HASH = "17b95f159c3e51440207cc6648f655201bac84fd0e1e5a9ad8461e2d7a2932d5"

# Mainnet genesis target 2^248 → byte[0]=0x01. work = 2^256/2^248 = 256.
MAINNET_TARGET = "01" + "00" * 31
MAINNET_WORK = 256

NODE_URL = "http://node.test"


def _node_info(tip_height: int = 4321) -> dict[str, Any]:
    return {
        "version": "v1.12.0",
        "network": "mainnet",
        "genesis_block_id": "ab" * 32,
        "tip_height": tip_height,
        "tip_block_id": BLOCK_HASH,
        "tip_age_seconds": 5,
        "peer_count": 3,
        "mempool_size": 0,
        "mempool_bytes": 0,
        "uptime_seconds": 1000,
        "metrics": {},
    }


def _block(height: int, timestamp: int, target: str = MAINNET_TARGET) -> dict[str, Any]:
    return {
        "hash": BLOCK_HASH,
        "height": height,
        "timestamp": timestamp,
        "tx_count": 1,
        "transactions": [TX_ID],
        "prev_block_id": "cd" * 32,
        "difficulty_target": target,
        "nonce": 42,
        "state_root": "ef" * 32,
        "tx_root": "12" * 32,
    }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_network_tools_registered_and_read_only() -> None:
    names = {
        "exfer_network_status",
        "exfer_network_hashrate",
        "exfer_get_block",
        "exfer_get_transaction",
    }
    registered = {t.name for t in TOOLS}
    assert names <= registered
    for n in names:
        assert n in HANDLERS
        # Read-only → dispatched without the walletd readiness gate.
        assert n in NO_WALLETD_TOOLS


# ---------------------------------------------------------------------------
# exfer_network_status
# ---------------------------------------------------------------------------


async def test_network_status_returns_node_info(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_rpc(method: str, params: object | None = None) -> object:
        assert method == "get_node_info"
        return _node_info(tip_height=999)

    monkeypatch.setattr(net, "node_rpc", fake_rpc)
    out = await HANDLERS["exfer_network_status"](None, {}, None)  # type: ignore[arg-type]
    parsed = json.loads(out[0].text)
    assert parsed["tip_height"] == 999
    assert parsed["network"] == "mainnet"


async def test_network_status_unreachable_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(method: str, params: object | None = None) -> object:
        raise NodeUnreachableError("no node reachable")

    monkeypatch.setattr(net, "node_rpc", boom)
    out = await HANDLERS["exfer_network_status"](None, {}, None)  # type: ignore[arg-type]
    assert "unreachable" in out[0].text.lower()
    assert "EXFER_NODE_RPC" in out[0].text


# ---------------------------------------------------------------------------
# exfer_network_hashrate
# ---------------------------------------------------------------------------


async def test_hashrate_uses_observed_window(monkeypatch: pytest.MonkeyPatch) -> None:
    # tip at height 100, window 10 → start at 90. Observed span 100s (10s/block,
    # exactly the target). est = work_per_block * window / span = 256*10/100.
    tip_h = 100
    window = 10

    async def fake_rpc(method: str, params: object | None = None) -> object:
        if method == "get_node_info":
            return _node_info(tip_height=tip_h)
        assert method == "get_block"
        assert isinstance(params, dict)
        h = params["height"]
        if h == tip_h:
            return _block(tip_h, timestamp=1000)
        if h == tip_h - window:
            return _block(tip_h - window, timestamp=900)
        raise AssertionError(f"unexpected height {h}")

    monkeypatch.setattr(net, "node_rpc", fake_rpc)
    out = await HANDLERS["exfer_network_hashrate"](None, {"window_blocks": window}, None)  # type: ignore[arg-type]
    parsed = json.loads(out[0].text)
    assert parsed["difficulty"] == MAINNET_TARGET
    assert parsed["work_per_block"] == MAINNET_WORK
    assert parsed["window_blocks"] == window
    assert parsed["window_seconds"] == 100
    assert parsed["tip_height"] == tip_h
    assert parsed["is_estimate"] is True
    # 256 hashes/block * 10 blocks / 100s = 25.6 H/s
    assert parsed["est_hashrate_hs"] == pytest.approx(25.6)


async def test_hashrate_nonpositive_span_falls_back_to_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tip_h = 50
    window = 5

    async def fake_rpc(method: str, params: object | None = None) -> object:
        if method == "get_node_info":
            return _node_info(tip_height=tip_h)
        # Both bounding blocks share a timestamp → span 0 → target fallback.
        return _block(int(params["height"]), timestamp=500)  # type: ignore[index]

    monkeypatch.setattr(net, "node_rpc", fake_rpc)
    out = await HANDLERS["exfer_network_hashrate"](None, {"window_blocks": window}, None)  # type: ignore[arg-type]
    parsed = json.loads(out[0].text)
    # span clamps to window * target; est = work / target_block_seconds.
    assert parsed["window_seconds"] == window * net.TARGET_BLOCK_TIME_SECS
    assert parsed["est_hashrate_hs"] == pytest.approx(MAINNET_WORK / net.TARGET_BLOCK_TIME_SECS)


async def test_hashrate_genesis_only_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_rpc(method: str, params: object | None = None) -> object:
        if method == "get_node_info":
            return _node_info(tip_height=0)
        return _block(0, timestamp=0)

    monkeypatch.setattr(net, "node_rpc", fake_rpc)
    out = await HANDLERS["exfer_network_hashrate"](None, {}, None)  # type: ignore[arg-type]
    parsed = json.loads(out[0].text)
    assert parsed["window_blocks"] == 0
    assert parsed["is_estimate"] is True
    assert parsed["est_hashrate_hs"] == pytest.approx(MAINNET_WORK / net.TARGET_BLOCK_TIME_SECS)


def test_work_from_target_math() -> None:
    # 2^256 / 2^248 = 256; 2^256 / 2^252 (testnet) = 16.
    assert net._work_from_target(MAINNET_TARGET) == 256
    testnet_target = "10" + "00" * 31  # 2^252
    assert net._work_from_target(testnet_target) == 16


# ---------------------------------------------------------------------------
# exfer_get_block
# ---------------------------------------------------------------------------


async def test_get_block_by_height_maps_param(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_rpc(method: str, params: object | None = None) -> object:
        seen["method"] = method
        seen["params"] = params
        return _block(7, timestamp=70)

    monkeypatch.setattr(net, "node_rpc", fake_rpc)
    out = await HANDLERS["exfer_get_block"](None, {"height": 7}, None)  # type: ignore[arg-type]
    assert seen == {"method": "get_block", "params": {"height": 7}}
    assert json.loads(out[0].text)["height"] == 7


async def test_get_block_by_hash_maps_to_hash_key(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_rpc(method: str, params: object | None = None) -> object:
        seen["params"] = params
        return _block(7, timestamp=70)

    monkeypatch.setattr(net, "node_rpc", fake_rpc)
    await HANDLERS["exfer_get_block"](None, {"block_id": BLOCK_HASH}, None)  # type: ignore[arg-type]
    # Node's param key is "hash", not "block_id".
    assert seen["params"] == {"hash": BLOCK_HASH}


async def test_get_block_rejects_both_and_neither(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_rpc(method: str, params: object | None = None) -> object:
        raise AssertionError("must not call the node on a bad request")

    monkeypatch.setattr(net, "node_rpc", fake_rpc)
    both = await HANDLERS["exfer_get_block"](  # type: ignore[arg-type]
        None, {"height": 1, "block_id": BLOCK_HASH}, None
    )
    assert "exactly one" in both[0].text
    neither = await HANDLERS["exfer_get_block"](None, {}, None)  # type: ignore[arg-type]
    assert "one of" in neither[0].text


# ---------------------------------------------------------------------------
# exfer_get_transaction
# ---------------------------------------------------------------------------


async def test_get_transaction_maps_tx_id_to_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_rpc(method: str, params: object | None = None) -> object:
        seen["method"] = method
        seen["params"] = params
        return {"tx_id": TX_ID, "tx_hex": "00", "in_mempool": True}

    monkeypatch.setattr(net, "node_rpc", fake_rpc)
    out = await HANDLERS["exfer_get_transaction"](None, {"tx_id": TX_ID}, None)  # type: ignore[arg-type]
    assert seen == {"method": "get_transaction", "params": {"hash": TX_ID}}
    assert json.loads(out[0].text)["tx_id"] == TX_ID


async def test_get_transaction_not_found_renders_code(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_rpc(method: str, params: object | None = None) -> object:
        raise NodeRpcError(-32602, "Transaction not found")

    monkeypatch.setattr(net, "node_rpc", fake_rpc)
    out = await HANDLERS["exfer_get_transaction"](None, {"tx_id": TX_ID}, None)  # type: ignore[arg-type]
    assert "-32602" in out[0].text
    assert "not found" in out[0].text.lower()


# ---------------------------------------------------------------------------
# node_fetch JSON-RPC client (against a respx-mocked HTTP node)
# ---------------------------------------------------------------------------


@pytest.fixture
def node_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXFER_NODE_RPC", NODE_URL)


def test_endpoints_parse_and_strip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXFER_NODE_RPC", " http://a:9334/ , http://b:9334 ")
    assert node_fetch.node_rpc_endpoints() == ["http://a:9334", "http://b:9334"]


async def test_node_rpc_returns_result(node_env: None) -> None:
    with respx.mock(assert_all_called=True) as router:
        router.post(NODE_URL).mock(
            return_value=httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 1, "result": _node_info()}
            )
        )
        res = await node_fetch.node_rpc("get_node_info")
    assert isinstance(res, dict)
    assert res["tip_height"] == 4321


async def test_node_rpc_maps_error_object(node_env: None) -> None:
    with respx.mock() as router:
        router.post(NODE_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": -32602, "message": "Invalid params: bad"},
                },
            )
        )
        with pytest.raises(NodeRpcError) as ei:
            await node_fetch.node_rpc("get_block", {"height": -1})
    assert ei.value.code == -32602


async def test_node_rpc_fails_over_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXFER_NODE_RPC", "http://dead.test,http://live.test")
    with respx.mock() as router:
        router.post("http://dead.test").mock(side_effect=httpx.ConnectError("refused"))
        router.post("http://live.test").mock(
            return_value=httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
            )
        )
        res = await node_fetch.node_rpc("get_node_info")
    assert res == {"ok": True}


async def test_node_rpc_all_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXFER_NODE_RPC", "http://dead1.test,http://dead2.test")
    with respx.mock() as router:
        router.post("http://dead1.test").mock(side_effect=httpx.ConnectError("refused"))
        router.post("http://dead2.test").mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(NodeUnreachableError):
            await node_fetch.node_rpc("get_node_info")
