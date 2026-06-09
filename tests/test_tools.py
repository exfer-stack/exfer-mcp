"""End-to-end tests for every v0.1 tool handler.

Each test wires the same mock walletd the SDK tests use, then drives
the tool handler exactly as ``server._call_tool`` would. The MCP
framework itself is not under test here — :mod:`mcp.server` is its
own project — but we do check the registry shape so a forgotten tool
or rename surfaces in CI.
"""

from __future__ import annotations

import json

import respx
from exfer_walletd import AsyncClient

from exfer_mcp.config import Config
from exfer_mcp.tools import HANDLERS, TOOLS

from .conftest import rpc_err, rpc_ok

ADDR = "8b0609a812de3b0103dd3b3e78be4a99ef1699b6f02820e1bc1dbcfc75681481"
ADDR2 = "11" * 32
TX_ID = "a02ab025d75a295540d681f89da3f8bfed894e02cea721085facbf9ad4525c68"
BLOCK_HASH = "17b95f159c3e51440207cc6648f655201bac84fd0e1e5a9ad8461e2d7a2932d5"

# `asyncio_mode = "auto"` in pyproject.toml auto-marks every `async def`
# test — no need to decorate explicitly. The three sync registry guards
# below stay sync and would otherwise warn under a blanket pytestmark.


# ---------------------------------------------------------------------------
# Registry — guards against accidental drift
# ---------------------------------------------------------------------------


def test_every_tool_has_a_handler() -> None:
    for tool in TOOLS:
        assert tool.name in HANDLERS, f"tool {tool.name!r} has no handler"
    assert set(HANDLERS) == {t.name for t in TOOLS}


def test_tool_names_are_exfer_prefixed() -> None:
    # Avoids name collisions with other MCP servers an agent has
    # configured at the same time — `exfer_transfer` is unambiguous in
    # a way `transfer` is not.
    for tool in TOOLS:
        assert tool.name.startswith("exfer_"), tool.name


def test_tool_count() -> None:
    # Bump when the tool surface changes — the count is a deliberate
    # signal that the API surface moved. v0.1 shipped 7; the agent
    # expansion (instant receipt, identity, HTLC, reputation) brought it
    # to 18 (naming was removed pending a datum-based redo); the
    # EXFER-QUOTE pair (quote_issue + quote_verify) brought it to 20; the
    # ergonomics pass (list_addresses + get_block_height) brought it to 22;
    # the honor read-back pair (get_output_datum +
    # find_settlements_by_quote_id) brought it to 24.
    # Every tool must have a handler.
    assert len(TOOLS) == 24
    assert len(HANDLERS) == len(TOOLS)
    assert {t.name for t in TOOLS} == set(HANDLERS)


# ---------------------------------------------------------------------------
# generate_address / get_balance
# ---------------------------------------------------------------------------


async def test_generate_address_returns_pubkey(
    client: AsyncClient, mock_walletd: respx.MockRouter, config: Config
) -> None:
    mock_walletd.post("/").mock(
        return_value=rpc_ok({"address": ADDR, "pubkey": "de" * 32, "index": 7})
    )
    out = await HANDLERS["exfer_generate_address"](client, {}, config)
    assert len(out) == 1
    parsed = json.loads(out[0].text)
    assert parsed == {"address": ADDR, "pubkey": "de" * 32, "index": 7}


async def test_list_addresses_returns_records(
    client: AsyncClient, mock_walletd: respx.MockRouter, config: Config
) -> None:
    records = [
        {"address": ADDR, "index": 0, "label": "default"},
        {"address": ADDR2, "index": 1, "imported": True},
    ]
    mock_walletd.post("/").mock(return_value=rpc_ok({"addresses": records}))
    out = await HANDLERS["exfer_list_addresses"](client, {}, config)
    parsed = json.loads(out[0].text)
    assert parsed == records


async def test_get_block_height_returns_height(
    client: AsyncClient, mock_walletd: respx.MockRouter, config: Config
) -> None:
    mock_walletd.post("/").mock(return_value=rpc_ok({"height": 577429, "block_id": BLOCK_HASH}))
    out = await HANDLERS["exfer_get_block_height"](client, {}, config)
    parsed = json.loads(out[0].text)
    assert parsed == {"height": 577429, "block_id": BLOCK_HASH}


async def test_get_balance(
    client: AsyncClient, mock_walletd: respx.MockRouter, config: Config
) -> None:
    mock_walletd.post("/").mock(return_value=rpc_ok({"address": ADDR, "balance": 99_900_000}))
    out = await HANDLERS["exfer_get_balance"](client, {"address": ADDR}, config)
    body = json.loads(out[0].text)
    assert body == {"address": ADDR, "balance": 99_900_000}


# ---------------------------------------------------------------------------
# simulate_transfer
# ---------------------------------------------------------------------------


async def test_simulate_transfer_round_trips_walletd_response(
    client: AsyncClient, mock_walletd: respx.MockRouter, config: Config
) -> None:
    walletd_resp = {
        "size": 250,
        "fee": 1_000,
        "fee_rate": 4,
        "inputs": [{"tx_id": TX_ID, "output_index": 1, "value": 100_000}],
        "outputs": [{"to": ADDR2, "amount": 50_000}],
        "total_in": 100_000,
        "total_out": 50_000,
        "change": 49_000,
        "built_at_height": 100,
    }
    route = mock_walletd.post("/").mock(return_value=rpc_ok(walletd_resp))
    out = await HANDLERS["exfer_simulate_transfer"](
        client,
        {
            "from_address": ADDR,
            "to_address": ADDR2,
            "amount": 50_000,
            "fee_rate": 4,
        },
        config,
    )
    parsed = json.loads(out[0].text)
    assert parsed == walletd_resp
    body = json.loads(route.calls.last.request.content)
    assert body["params"]["outputs"] == [{"to": ADDR2, "amount": 50_000}]


async def test_simulate_transfer_uses_config_default_fee_rate(
    client: AsyncClient, mock_walletd: respx.MockRouter
) -> None:
    cfg = Config(
        walletd_url="http://walletd.test",
        walletd_token="test-token",
        walletd_fingerprint=None,
        default_fee_rate=8,
        httpx_timeout=30.0,
    )
    route = mock_walletd.post("/").mock(
        return_value=rpc_ok(
            {
                "size": 250,
                "fee": 2_000,
                "fee_rate": 8,
                "inputs": [],
                "outputs": [{"to": ADDR2, "amount": 50_000}],
                "total_in": 100_000,
                "total_out": 50_000,
                "change": 48_000,
                "built_at_height": 100,
            }
        )
    )
    await HANDLERS["exfer_simulate_transfer"](
        client,
        {"from_address": ADDR, "to_address": ADDR2, "amount": 50_000},
        cfg,
    )
    body = json.loads(route.calls.last.request.content)
    assert body["params"]["fee_rate"] == 8


# ---------------------------------------------------------------------------
# transfer + InsufficientBalanceError surfacing
# ---------------------------------------------------------------------------


async def test_transfer_success(
    client: AsyncClient, mock_walletd: respx.MockRouter, config: Config
) -> None:
    walletd_resp = {
        "tx_id": TX_ID,
        "size": 250,
        "tip_height": 100,
        "submitted": True,
    }
    mock_walletd.post("/").mock(return_value=rpc_ok(walletd_resp))
    out = await HANDLERS["exfer_transfer"](
        client,
        {"from_address": ADDR, "to_address": ADDR2, "amount": 50_000},
        config,
    )
    parsed = json.loads(out[0].text)
    assert parsed["tx_id"] == TX_ID


async def test_transfer_insufficient_balance_renders_plain_english(
    client: AsyncClient, mock_walletd: respx.MockRouter, config: Config
) -> None:
    mock_walletd.post("/").mock(
        return_value=rpc_err(
            -32031,
            "insufficient balance: have 100, need 50000 (already reserved by pending transfers)",
        )
    )
    out = await HANDLERS["exfer_transfer"](
        client,
        {"from_address": ADDR, "to_address": ADDR2, "amount": 50_000},
        config,
    )
    msg = out[0].text
    assert "insufficient balance" in msg.lower()
    assert "-32031" in msg


# ---------------------------------------------------------------------------
# quote_issue / quote_verify (EXFER-QUOTE)
# ---------------------------------------------------------------------------


async def test_quote_issue_round_trips_walletd_response(
    client: AsyncClient, mock_walletd: respx.MockRouter, config: Config
) -> None:
    quote = {
        "version": 1,
        "quote_id": "ab" * 16,
        "currency": "USD",
        "amount_minor": 1299,
        "rate_exfers_per_unit": 1000,
        "exfer_amount": 1_299_000,
        "payee_pubkey": "cd" * 32,
        "issued_at": 1_733_600_000,
        "expires_at": 1_733_600_600,
        "memo": "coffee",
        "signer_pubkey": "ef" * 32,
        "signature": "ab" * 64,
    }
    walletd_resp = {
        "quote": quote,
        "signature": "ab" * 64,
        "signer_address": ADDR,
        "payee_address": ADDR2,
        "genesis_block_id": BLOCK_HASH,
        "image": "deadbeef",
        "htlc_preimage": "11" * 32,
        "htlc_hash_lock": "22" * 32,
    }
    route = mock_walletd.post("/").mock(return_value=rpc_ok(walletd_resp))
    out = await HANDLERS["exfer_quote_issue"](
        client,
        {
            "address": ADDR,
            "payee_pubkey": "cd" * 32,
            "currency": "USD",
            "amount_minor": 1299,
            "rate_exfers_per_unit": 1000,
            "exfer_amount": 1_299_000,
            "ttl_secs": 600,
            "memo": "coffee",
        },
        config,
    )
    parsed = json.loads(out[0].text)
    assert parsed == walletd_resp
    # optional args are forwarded; absent ones are omitted from the RPC params
    body = json.loads(route.calls.last.request.content)
    assert body["params"]["address"] == ADDR
    assert body["params"]["ttl_secs"] == 600
    assert body["params"]["memo"] == "coffee"
    assert "payer_pubkey" not in body["params"]
    assert "quote_id" not in body["params"]


async def test_quote_issue_scope_denied_renders_error(
    client: AsyncClient, mock_walletd: respx.MockRouter, config: Config
) -> None:
    # A Read-scoped token hitting the Spend-posture issue RPC must fail at
    # walletd (-32001) and be surfaced by render_error — there is no
    # MCP-side capability check by design.
    mock_walletd.post("/").mock(return_value=rpc_err(-32001, "insufficient scope: need Spend"))
    out = await HANDLERS["exfer_quote_issue"](
        client,
        {
            "address": ADDR,
            "payee_pubkey": "cd" * 32,
            "currency": "USD",
            "amount_minor": 1299,
            "rate_exfers_per_unit": 1000,
            "exfer_amount": 1_299_000,
            "ttl_secs": 600,
        },
        config,
    )
    assert "-32001" in out[0].text


async def test_quote_verify_round_trips_walletd_response(
    client: AsyncClient, mock_walletd: respx.MockRouter, config: Config
) -> None:
    quote = {
        "version": 1,
        "quote_id": "ab" * 16,
        "currency": "USD",
        "amount_minor": 1299,
        "rate_exfers_per_unit": 1000,
        "exfer_amount": 1_299_000,
        "payee_pubkey": "cd" * 32,
        "issued_at": 1_733_600_000,
        "expires_at": 1_733_600_600,
        "memo": "coffee",
        "signer_pubkey": "ef" * 32,
        "signature": "ab" * 64,
    }
    walletd_resp = {
        "valid": True,
        "signer_address": ADDR,
        "payee_address": ADDR2,
        "genesis_block_id": BLOCK_HASH,
    }
    route = mock_walletd.post("/").mock(return_value=rpc_ok(walletd_resp))
    out = await HANDLERS["exfer_quote_verify"](client, {"quote": quote}, config)
    parsed = json.loads(out[0].text)
    assert parsed == walletd_resp
    # the quote object is forwarded verbatim under the `quote` param
    body = json.loads(route.calls.last.request.content)
    assert body["params"]["quote"] == quote


# ---------------------------------------------------------------------------
# wait_for_tx + WaitTimeoutError
# ---------------------------------------------------------------------------


async def test_wait_for_tx_success(
    client: AsyncClient, mock_walletd: respx.MockRouter, config: Config
) -> None:
    mock_walletd.post("/").mock(
        return_value=rpc_ok(
            {
                "tx_id": TX_ID,
                "block_id": BLOCK_HASH,
                "block_height": 100,
                "confirmations": 1,
            }
        )
    )
    out = await HANDLERS["exfer_wait_for_tx"](client, {"tx_id": TX_ID}, config)
    parsed = json.loads(out[0].text)
    assert parsed["confirmations"] == 1


async def test_wait_for_tx_timeout_renders_friendly_text(
    client: AsyncClient, mock_walletd: respx.MockRouter, config: Config
) -> None:
    mock_walletd.post("/").mock(return_value=rpc_err(-32040, "wait_for_tx: timed out"))
    out = await HANDLERS["exfer_wait_for_tx"](client, {"tx_id": TX_ID, "timeout_secs": 10}, config)
    msg = out[0].text
    assert "not a terminal failure" in msg.lower()
    assert "-32040" in msg


# ---------------------------------------------------------------------------
# payment_uri_encode / payment_uri_decode
# ---------------------------------------------------------------------------


async def test_payment_uri_encode_returns_bare_string(
    client: AsyncClient, mock_walletd: respx.MockRouter, config: Config
) -> None:
    expected = f"exfer:{ADDR}?amount=100000000&memo=invoice%2099"
    mock_walletd.post("/").mock(return_value=rpc_ok({"uri": expected}))
    out = await HANDLERS["exfer_payment_uri_encode"](
        client,
        {"address": ADDR, "amount": 100_000_000, "memo": "invoice 99"},
        config,
    )
    assert out[0].text == expected


async def test_payment_uri_decode_returns_json(
    client: AsyncClient, mock_walletd: respx.MockRouter, config: Config
) -> None:
    mock_walletd.post("/").mock(
        return_value=rpc_ok({"address": ADDR, "amount": 100_000_000, "memo": "invoice 99"})
    )
    out = await HANDLERS["exfer_payment_uri_decode"](
        client, {"uri": f"exfer:{ADDR}?amount=100000000&memo=invoice%2099"}, config
    )
    parsed = json.loads(out[0].text)
    assert parsed["address"] == ADDR
    assert parsed["amount"] == 100_000_000


# ---------------------------------------------------------------------------
# simulate_transfer with datum (honor settlement dry-run parity)
# ---------------------------------------------------------------------------


async def test_simulate_transfer_forwards_datum(
    client: AsyncClient, mock_walletd: respx.MockRouter, config: Config
) -> None:
    # A honor settlement carries the 16-byte quote_id as an inline datum;
    # the dry-run must forward those bytes so size/fee match the real
    # transfer (245 bytes with a 16-byte datum vs 227 without).
    quote_datum = "ab" * 16
    walletd_resp = {
        "size": 245,
        "fee": 1_000,
        "fee_rate": 4,
        "inputs": [{"tx_id": TX_ID, "output_index": 1, "value": 100_000}],
        "outputs": [{"to": ADDR2, "amount": 50_000}],
        "total_in": 100_000,
        "total_out": 50_000,
        "change": 49_000,
        "built_at_height": 100,
    }
    route = mock_walletd.post("/").mock(return_value=rpc_ok(walletd_resp))
    out = await HANDLERS["exfer_simulate_transfer"](
        client,
        {
            "from_address": ADDR,
            "to_address": ADDR2,
            "amount": 50_000,
            "fee_rate": 4,
            "datum": quote_datum,
        },
        config,
    )
    parsed = json.loads(out[0].text)
    assert parsed == walletd_resp
    body = json.loads(route.calls.last.request.content)
    assert body["params"]["datum"] == quote_datum


async def test_simulate_transfer_omits_datum_when_absent(
    client: AsyncClient, mock_walletd: respx.MockRouter, config: Config
) -> None:
    route = mock_walletd.post("/").mock(
        return_value=rpc_ok(
            {
                "size": 227,
                "fee": 1_000,
                "fee_rate": 4,
                "inputs": [],
                "outputs": [{"to": ADDR2, "amount": 50_000}],
                "total_in": 100_000,
                "total_out": 50_000,
                "change": 49_000,
                "built_at_height": 100,
            }
        )
    )
    await HANDLERS["exfer_simulate_transfer"](
        client,
        {"from_address": ADDR, "to_address": ADDR2, "amount": 50_000, "fee_rate": 4},
        config,
    )
    body = json.loads(route.calls.last.request.content)
    assert "datum" not in body["params"]


# ---------------------------------------------------------------------------
# get_output_datum / find_settlements_by_quote_id (honor read-back)
# ---------------------------------------------------------------------------


async def test_get_output_datum_returns_quote_id(
    client: AsyncClient, mock_walletd: respx.MockRouter, config: Config
) -> None:
    quote_id = "ab" * 16
    route = mock_walletd.post("/").mock(
        return_value=rpc_ok({"quote_id": quote_id, "unhonorable": False})
    )
    out = await HANDLERS["exfer_get_output_datum"](
        client, {"tx_id": TX_ID, "output_index": 0}, config
    )
    parsed = json.loads(out[0].text)
    assert parsed == {"quote_id": quote_id, "unhonorable": False}
    body = json.loads(route.calls.last.request.content)
    assert body["method"] == "get_output_datum"
    assert body["params"] == {"tx_id": TX_ID, "output_index": 0}


async def test_get_output_datum_unhonorable_passthrough(
    client: AsyncClient, mock_walletd: respx.MockRouter, config: Config
) -> None:
    # A datum_hash-only output: bytes never published inline.
    mock_walletd.post("/").mock(return_value=rpc_ok({"quote_id": None, "unhonorable": True}))
    out = await HANDLERS["exfer_get_output_datum"](
        client, {"tx_id": TX_ID, "output_index": 2}, config
    )
    parsed = json.loads(out[0].text)
    assert parsed == {"quote_id": None, "unhonorable": True}


async def test_get_output_datum_indexer_not_configured(
    client: AsyncClient, mock_walletd: respx.MockRouter, config: Config
) -> None:
    mock_walletd.post("/").mock(
        return_value=rpc_err(-32050, "indexer not configured: start walletd with --indexer-rpc")
    )
    out = await HANDLERS["exfer_get_output_datum"](
        client, {"tx_id": TX_ID, "output_index": 0}, config
    )
    assert "indexer" in out[0].text.lower()
    assert "-32050" in out[0].text


async def test_find_settlements_by_quote_id_returns_outpoints(
    client: AsyncClient, mock_walletd: respx.MockRouter, config: Config
) -> None:
    quote_id = "cd" * 16
    settlements = [
        {"tx_id": TX_ID, "output_index": 0},
        {"tx_id": "ff" * 32, "output_index": 3},
    ]
    route = mock_walletd.post("/").mock(return_value=rpc_ok({"settlements": settlements}))
    out = await HANDLERS["exfer_find_settlements_by_quote_id"](
        client, {"quote_id": quote_id}, config
    )
    parsed = json.loads(out[0].text)
    assert parsed == {"settlements": settlements}
    body = json.loads(route.calls.last.request.content)
    assert body["method"] == "find_settlements_by_quote_id"
    assert body["params"] == {"quote_id": quote_id}


async def test_find_settlements_by_quote_id_empty(
    client: AsyncClient, mock_walletd: respx.MockRouter, config: Config
) -> None:
    mock_walletd.post("/").mock(return_value=rpc_ok({"settlements": []}))
    out = await HANDLERS["exfer_find_settlements_by_quote_id"](
        client, {"quote_id": "cd" * 16}, config
    )
    parsed = json.loads(out[0].text)
    assert parsed == {"settlements": []}
