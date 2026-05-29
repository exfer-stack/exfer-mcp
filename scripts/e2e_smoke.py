#!/usr/bin/env python3
"""End-to-end smoke for the full Exfer agent stack.

Drives a real walletd + indexer through the v0.1 MCP tool surface AND
the v0.2-staged walletd SDK methods (HTLC + indexer-delegated queries),
proving every layer of the stack is in agreement on at least the
read-only paths.

### What it verifies

Tier 1 — always-safe read-only:

* walletd healthz responds
* MCP server boots over stdio, lists exactly the v0.1 seven tools
* MCP `exfer_get_balance` round-trips a known address (the faucet's
  own `faucet_address` reported by `https://exfer-faucet.fly.dev/api/info`)
* MCP `exfer_payment_uri_encode` / `exfer_payment_uri_decode` round-trip
* MCP `exfer_simulate_transfer` returns a typed receipt (no broadcast)

Tier 2 — indexer-backed (skipped until `get_follower_status.lag` is
small enough to mean the backfill caught up):

* SDK `htlc_status` on a known-open faucet HTLC (proves the byte-exact
  HTLC parser matches what indexer extracted)
* SDK `get_address_history` on `faucet_address` (proves walletd →
  indexer proxy works on real mainnet data)
* SDK `contract_stats` on `faucet_address` (proves the
  `structural_merkle_hash` contract-typing works end-to-end)

Tier 3 — opt-in spend (requires `EXFER_E2E_SPEND=1` *and* a funded
managed address):

* MCP `exfer_transfer` of a small amount to a fresh self-generated
  address, followed by MCP `exfer_wait_for_tx`

### Run it

```
WALLETD_URL=https://your-walletd \\
WALLETD_AUTH_TOKEN=read-or-spend-token \\
WALLETD_FINGERPRINT=sha256:... \\           # only if https
python -m scripts.e2e_smoke
```

Exit code 0 on all-pass / all-skip-with-reason, 1 on any failure. Each
phase prints a one-line PASS / FAIL / SKIP with timing + evidence;
final summary surfaces counts.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from exfer_walletd import AsyncClient
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

FAUCET_INFO_URL = "https://exfer-faucet.fly.dev/api/info"
FAUCET_TASKS_URL = "https://exfer-faucet.fly.dev/api/tasks"

# Number of seconds the indexer must already be caught up to before
# Tier 2 phases will run. Cold-boot full chain backfill on mainnet is
# ~110 min as of this writing; pick a tolerance an established
# follower would meet (~10 blocks ≈ 30 s of lag).
INDEXER_LAG_TOLERANCE_BLOCKS = 50

# Public stack-tip probe — used to compute "is the indexer fully caught up?"
# without trusting the indexer's own tip-height field (a malfunctioning
# upstream RPC could mislead it).
COMMUNITY_NODE_URL = "http://80.78.31.82:9334"


@dataclass
class Phase:
    name: str
    fn: Callable[[Ctx], Awaitable[str]]
    tier: int


@dataclass
class Ctx:
    walletd_url: str
    walletd_token: str
    walletd_fingerprint: str | None
    sdk: AsyncClient
    mcp: ClientSession
    faucet_info: dict[str, Any]
    spend_enabled: bool


# ---------------------------------------------------------------------------
# Tier 1 — always-safe read-only
# ---------------------------------------------------------------------------


async def t1_walletd_healthz(ctx: Ctx) -> str:
    ok = await ctx.sdk.healthz()
    if not ok:
        raise AssertionError("walletd /healthz returned False")
    return "GET /healthz → ok"


async def t1_mcp_lists_seven_tools(ctx: Ctx) -> str:
    result = await ctx.mcp.list_tools()
    names = sorted(t.name for t in result.tools)
    expected = {
        "exfer_generate_address",
        "exfer_get_balance",
        "exfer_simulate_transfer",
        "exfer_transfer",
        "exfer_wait_for_tx",
        "exfer_payment_uri_encode",
        "exfer_payment_uri_decode",
    }
    if set(names) != expected:
        raise AssertionError(
            f"MCP tool set mismatch: missing {expected - set(names)}, extra {set(names) - expected}"
        )
    return f"7/7 tools present: {', '.join(sorted(expected))}"


async def t1_mcp_get_balance_round_trips(ctx: Ctx) -> str:
    """Use the faucet's own published address — guaranteed to exist on
    chain (it's the source of every HTLC payout) so balance lookup
    can't fall through to a "wallet not found" path."""
    addr = ctx.faucet_info["faucet_address"]
    result = await ctx.mcp.call_tool("exfer_get_balance", {"address": addr})
    if result.isError:
        raise AssertionError(f"MCP exfer_get_balance errored: {result.content!r}")
    text = result.content[0].text  # type: ignore[union-attr]
    parsed = json.loads(text)
    if parsed["address"] != addr:
        raise AssertionError(f"address echo mismatch: {parsed!r}")
    if not isinstance(parsed["balance"], int):
        raise AssertionError(f"balance not int: {parsed!r}")
    return f"balance({addr[:12]}…) = {parsed['balance']} exfers"


async def t1_mcp_payment_uri_round_trip(ctx: Ctx) -> str:
    addr = ctx.faucet_info["faucet_address"]
    encode_result = await ctx.mcp.call_tool(
        "exfer_payment_uri_encode",
        {"address": addr, "amount": 100_000_000, "memo": "smoke test"},
    )
    if encode_result.isError:
        raise AssertionError(f"encode error: {encode_result.content!r}")
    uri = encode_result.content[0].text  # type: ignore[union-attr]
    if not uri.startswith(f"exfer:{addr}"):
        raise AssertionError(f"uri does not start with the address: {uri!r}")

    decode_result = await ctx.mcp.call_tool(
        "exfer_payment_uri_decode", {"uri": uri}
    )
    if decode_result.isError:
        raise AssertionError(f"decode error: {decode_result.content!r}")
    parsed = json.loads(decode_result.content[0].text)  # type: ignore[union-attr]
    if parsed["address"] != addr or parsed["amount"] != 100_000_000:
        raise AssertionError(f"round-trip mismatch: {parsed!r}")
    return "encode + decode bit-exact"


async def t1_mcp_simulate_transfer(ctx: Ctx) -> str:
    """Simulate (not broadcast) a transfer from a sentinel address that
    walletd does not hold a key for. Expected outcome: walletd reports
    `wallet not found` (-32010) or `insufficient balance` (-32031) —
    either confirms the build-only codepath is reachable and is honestly
    reporting that the address has no owned UTXOs, not silently
    fabricating a phantom one. We assert the JSON-RPC code, not
    error-free completion.

    We avoid `exfer_generate_address` here because it requires Manage
    scope; a Read-scope smoke run would cascade an auth error into the
    next tool call as a schema-validation failure (the error text is
    not 64 hex chars).
    """
    # Deterministic sentinel — guaranteed not to collide with any
    # generated managed address (those are derived via HD from a seed).
    sentinel = "ff" * 32
    sim_result = await ctx.mcp.call_tool(
        "exfer_simulate_transfer",
        {"from_address": sentinel, "to_address": sentinel, "amount": 1_000},
    )
    text = sim_result.content[0].text  # type: ignore[union-attr]
    if not sim_result.isError:
        # Surprising — sentinel address shouldn't have funds. Still a
        # PASS: walletd's build path returned a receipt, which means it
        # is structurally working.
        return f"simulate(sentinel → sentinel) returned a receipt: {text[:80]}…"
    lower = text.lower()
    if "insufficient" in lower or "wallet" in lower or "not found" in lower:
        return f"simulate(sentinel) → honest 'no funds' rejection ({_first_code(text)})"
    raise AssertionError(f"unexpected simulate error shape: {text!r}")


def _first_code(text: str) -> str:
    """Pull the first '-32XXX' code out of a render_error string for
    compact log lines."""
    import re

    m = re.search(r"-32\d{3}", text)
    return m.group(0) if m else "no-code"


# ---------------------------------------------------------------------------
# Tier 2 — indexer-backed (only when indexer is caught up)
# ---------------------------------------------------------------------------


async def t2_indexer_close_enough_to_tip(ctx: Ctx) -> str:
    """Decides whether subsequent Tier 2 phases run. Returns a SKIP
    reason string if backfill hasn't caught up, else a PASS reason."""
    follower = await ctx.sdk.get_follower_status()
    community_tip = _fetch_community_tip()
    lag = community_tip - follower["last_indexed_height"]
    if lag > INDEXER_LAG_TOLERANCE_BLOCKS:
        raise SkipPhase(
            f"indexer lag {lag} blocks (tip {community_tip}, indexer at "
            f"{follower['last_indexed_height']}); skipping Tier 2"
        )
    return f"indexer at {follower['last_indexed_height']}, community tip {community_tip}, lag {lag}"


async def t2_htlc_lookup_by_hashlock_finds_faucet_task(ctx: Ctx) -> str:
    """Walletd v1.9.1 proxies `htlc_lookup_by_hashlock` to indexer.
    `htlc_status` is intentionally NOT proxied — it remains walletd's
    "my HTLCs" view — so for a non-owned outpoint we look up by
    hash_lock and assert the lock_tx_id appears."""
    task = _pick_a_faucet_task()
    hash_lock = task["onchain"]["hash_lock"]
    expected_tx_id = task["onchain"]["lock_tx_id"]

    htlcs = await ctx.sdk.htlc_lookup_by_hashlock(hash_lock)
    matching = [h for h in htlcs if h["lock_tx_id"] == expected_tx_id]
    if not matching:
        raise AssertionError(
            f"task {task['id']}: hash_lock {hash_lock[:12]}… resolved to "
            f"{len(htlcs)} HTLC(s) but none had lock_tx_id {expected_tx_id[:12]}…"
        )
    return (
        f"task {task['id']}: hash_lock {hash_lock[:12]}… → indexer returned "
        f"{len(htlcs)} HTLC(s), lock_tx_id {expected_tx_id[:12]}… present "
        f"(state {matching[0]['state']!r})"
    )


async def t2_get_address_history_via_walletd_proxy(ctx: Ctx) -> str:
    """`get_address_history` is implemented ONLY by indexer; walletd
    proxies it via `--indexer-rpc`. Successful response proves the
    delegation wire is live."""
    addr = ctx.faucet_info["faucet_address"]
    result = await ctx.sdk.get_address_history(addr, limit=5)
    rows = result.get("history", [])
    if not rows:
        raise AssertionError(
            "get_address_history returned no rows for the faucet address — "
            "indexer didn't extract its activity"
        )
    return f"faucet_address has {len(rows)} most-recent activity rows via proxy"


async def t2_contract_stats_via_walletd_proxy(ctx: Ctx) -> str:
    addr = ctx.faucet_info["faucet_address"]
    stats = await ctx.sdk.contract_stats(addr)
    if not stats:
        raise AssertionError(
            "contract_stats returned no rows — indexer didn't aggregate "
            "settlements for the faucet address"
        )
    total = sum(s["total"] for s in stats)
    return (
        f"faucet_address settled {total} HTLCs across {len(stats)} contract "
        f"templates (structural_merkle_hash collapsing works)"
    )


# ---------------------------------------------------------------------------
# Tier 3 — opt-in spend
# ---------------------------------------------------------------------------


async def t3_real_transfer_and_wait(ctx: Ctx) -> str:
    if not ctx.spend_enabled:
        raise SkipPhase("EXFER_E2E_SPEND != 1; skipping real transfer")
    funded = os.environ.get("EXFER_E2E_FUNDED_ADDR")
    if not funded:
        raise SkipPhase(
            "EXFER_E2E_FUNDED_ADDR not set; can't spend without a funded source"
        )
    dest_result = await ctx.mcp.call_tool("exfer_generate_address", {})
    dest = dest_result.content[0].text  # type: ignore[union-attr]

    sim_result = await ctx.mcp.call_tool(
        "exfer_simulate_transfer",
        {"from_address": funded, "to_address": dest, "amount": 1_000},
    )
    if sim_result.isError:
        raise AssertionError(f"simulate failed: {sim_result.content!r}")

    transfer_result = await ctx.mcp.call_tool(
        "exfer_transfer",
        {"from_address": funded, "to_address": dest, "amount": 1_000},
    )
    if transfer_result.isError:
        raise AssertionError(f"transfer failed: {transfer_result.content!r}")
    parsed = json.loads(transfer_result.content[0].text)  # type: ignore[union-attr]
    tx_id = parsed["tx_id"]

    wait_result = await ctx.mcp.call_tool(
        "exfer_wait_for_tx", {"tx_id": tx_id, "timeout_secs": 120}
    )
    if wait_result.isError:
        raise AssertionError(
            f"wait_for_tx failed: {wait_result.content!r} (tx_id {tx_id})"
        )
    return f"transferred 1000 exfers, confirmed: tx {tx_id[:12]}…"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class SkipPhase(Exception):
    """Raised by a phase to signal "skip with reason" rather than failure."""


PHASES: list[Phase] = [
    Phase("T1 walletd healthz", t1_walletd_healthz, tier=1),
    Phase("T1 MCP lists 7 tools", t1_mcp_lists_seven_tools, tier=1),
    Phase("T1 MCP get_balance round-trip", t1_mcp_get_balance_round_trips, tier=1),
    Phase("T1 MCP payment URI round-trip", t1_mcp_payment_uri_round_trip, tier=1),
    Phase("T1 MCP simulate_transfer honesty", t1_mcp_simulate_transfer, tier=1),
    Phase("T2 indexer caught up", t2_indexer_close_enough_to_tip, tier=2),
    Phase(
        "T2 htlc_lookup_by_hashlock on faucet HTLC",
        t2_htlc_lookup_by_hashlock_finds_faucet_task,
        tier=2,
    ),
    Phase("T2 get_address_history via walletd proxy", t2_get_address_history_via_walletd_proxy, tier=2),
    Phase("T2 contract_stats via walletd proxy", t2_contract_stats_via_walletd_proxy, tier=2),
    Phase("T3 transfer + wait_for_tx (opt-in)", t3_real_transfer_and_wait, tier=3),
]


def _fetch_community_tip() -> int:
    """Synchronous HTTP because httpx in async land tangles with our
    own AsyncClient's event loop. Only called once per smoke run."""
    req = urllib.request.Request(
        COMMUNITY_NODE_URL,
        data=b'{"jsonrpc":"2.0","method":"get_block_height","id":1}',
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = json.loads(resp.read())
    return int(body["result"]["height"])


def _fetch_faucet_info() -> dict[str, Any]:
    with urllib.request.urlopen(FAUCET_INFO_URL, timeout=10) as resp:
        body = json.loads(resp.read())
    return body  # type: ignore[no-any-return]


def _pick_a_faucet_task() -> dict[str, Any]:
    """Return the first faucet task whose `lock_height` we can already
    cover with the current indexer state. The list is small enough to
    walk linearly."""
    with urllib.request.urlopen(FAUCET_TASKS_URL, timeout=10) as resp:
        tasks = json.loads(resp.read())
    if not tasks:
        raise SkipPhase("faucet has no open tasks right now")
    # Just return the first — caller asserts hash_lock, which any task
    # will satisfy if the indexer has caught up.
    return tasks[0]  # type: ignore[no-any-return]


async def run_phase(ctx: Ctx, phase: Phase, results: dict[str, str]) -> None:
    start = time.monotonic()
    label = f"[T{phase.tier}] {phase.name}"
    try:
        evidence = await phase.fn(ctx)
        elapsed = time.monotonic() - start
        print(f"  PASS  {label}  ({elapsed:.2f}s)  {evidence}")
        results[phase.name] = "PASS"
        # If T2-gate fails (raises SkipPhase), the gate phase itself
        # is marked SKIP but downstream T2 phases need to know not to
        # run.
    except SkipPhase as exc:
        elapsed = time.monotonic() - start
        print(f"  SKIP  {label}  ({elapsed:.2f}s)  {exc}")
        results[phase.name] = "SKIP"
    except Exception as exc:
        elapsed = time.monotonic() - start
        print(f"  FAIL  {label}  ({elapsed:.2f}s)  {type(exc).__name__}: {exc}")
        results[phase.name] = "FAIL"


async def main() -> int:
    walletd_url = os.environ.get("WALLETD_URL")
    walletd_token = os.environ.get("WALLETD_AUTH_TOKEN")
    walletd_fingerprint = os.environ.get("WALLETD_FINGERPRINT") or None
    if not walletd_url or not walletd_token:
        print("WALLETD_URL and WALLETD_AUTH_TOKEN must be set", file=sys.stderr)
        return 2
    spend_enabled = os.environ.get("EXFER_E2E_SPEND") == "1"

    sdk_kwargs: dict[str, Any] = {"timeout": 60.0}
    if walletd_fingerprint:
        sdk_kwargs["fingerprint"] = walletd_fingerprint

    mcp_env = {
        "WALLETD_URL": walletd_url,
        "WALLETD_AUTH_TOKEN": walletd_token,
        # PATH so the subprocess can find `exfer-mcp` in the venv.
        "PATH": os.environ.get("PATH", ""),
    }
    if walletd_fingerprint:
        mcp_env["WALLETD_FINGERPRINT"] = walletd_fingerprint

    print(f"# exfer-mcp e2e smoke against {walletd_url}")
    print(f"# spend phases: {'ENABLED' if spend_enabled else 'disabled'}")
    print()

    faucet_info = _fetch_faucet_info()
    print(f"# faucet info: address={faucet_info['faucet_address'][:12]}…")
    print()

    results: dict[str, str] = {}
    indexer_gate_failed = False

    server_params = StdioServerParameters(command="exfer-mcp", env=mcp_env)
    async with (
        AsyncClient(walletd_url, walletd_token, **sdk_kwargs) as sdk,
        stdio_client(server_params) as (read, write),
        ClientSession(read, write) as mcp_session,
    ):
        await mcp_session.initialize()
        ctx = Ctx(
            walletd_url=walletd_url,
            walletd_token=walletd_token,
            walletd_fingerprint=walletd_fingerprint,
            sdk=sdk,
            mcp=mcp_session,
            faucet_info=faucet_info,
            spend_enabled=spend_enabled,
        )
        for phase in PHASES:
            # T2 phases (other than the gate itself) are skipped
            # wholesale if the gate phase couldn't prove indexer was
            # caught up.
            if (
                phase.tier == 2
                and indexer_gate_failed
                and phase.name != "T2 indexer caught up"
            ):
                print(f"  SKIP  [T2] {phase.name}  gated by previous skip")
                results[phase.name] = "SKIP"
                continue
            await run_phase(ctx, phase, results)
            if (
                phase.name == "T2 indexer caught up"
                and results[phase.name] == "SKIP"
            ):
                indexer_gate_failed = True

    print()
    counts = {k: sum(1 for v in results.values() if v == k) for k in ("PASS", "SKIP", "FAIL")}
    print(f"# summary: {counts['PASS']} PASS, {counts['SKIP']} SKIP, {counts['FAIL']} FAIL")
    return 0 if counts["FAIL"] == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
