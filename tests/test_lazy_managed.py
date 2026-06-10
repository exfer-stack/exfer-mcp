"""Lazy / non-blocking MANAGED-mode contract tests.

The load-bearing property: in MANAGED mode the MCP server must serve the
handshake + ``list_tools`` IMMEDIATELY, without waiting for the managed
walletd to boot. Readiness is gated lazily on the first TOOL call via
``WalletdSupervisor.ensure_ready()``.

These tests never spawn a real walletd. They drive the async readiness
primitive and the ``server.build_server`` dispatch directly with stubs /
patched supervisors, so a slow (or never-ready) walletd is simulated
deterministically and the timing assertions stay fast and stable.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import mcp.types as mcp_types
import pytest

from exfer_mcp.config import Config, ConfigError, ManagedConfig
from exfer_mcp.server import build_server
from exfer_mcp.walletd_supervisor import WalletdSupervisor

# A bring-up that never resolves should still let the handshake through;
# we cap the test's own patience well under that so a regression (a
# blocking handshake) fails fast instead of hanging the suite.
_HANDSHAKE_BUDGET_SECS = 1.0


def _managed_cfg(tmp_path: Path, port: int = 7448) -> ManagedConfig:
    return ManagedConfig(
        binary="/nonexistent/exfer-walletd",  # never actually spawned here
        keystore_passphrase="throwaway",
        node_rpc="http://node.test:9334",
        indexer_rpc=None,
        datadir=tmp_path / "dd",
        bind_host="127.0.0.1",
        bind_port=port,
    )


class _Tip:
    def __init__(self, height: int, block_id: str) -> None:
        self.height = height
        self.block_id = block_id


class _FakeClient:
    """Stand-in for exfer.AsyncClient at the tool-dispatch layer."""

    def __init__(self) -> None:
        self.tip_calls = 0

    async def get_tip(self) -> _Tip:
        self.tip_calls += 1
        return _Tip(4242, "ab" * 32)


# ---------------------------------------------------------------------------
# list_tools / handshake is non-blocking even while walletd is still booting
# ---------------------------------------------------------------------------


async def _invoke_list_tools(server: Any) -> list[mcp_types.Tool]:
    handler = server.request_handlers[mcp_types.ListToolsRequest]
    req = mcp_types.ListToolsRequest(method="tools/list")
    result = await handler(req)
    return list(result.root.tools)


async def _invoke_call_tool(
    server: Any, name: str, arguments: dict[str, Any]
) -> mcp_types.CallToolResult:
    handler = server.request_handlers[mcp_types.CallToolRequest]
    req = mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name=name, arguments=arguments),
    )
    result = await handler(req)
    return result.root


async def test_list_tools_returns_immediately_while_walletd_not_ready() -> None:
    """The handshake MUST NOT await readiness.

    The provider here both (a) never resolves and (b) asserts if ever
    awaited — so a list_tools that touched the provider would either hang
    (caught by the timeout) or raise. A correct list_tools returns the
    full tool surface with zero wait.
    """
    provider_called = False

    async def never_ready_provider() -> tuple[Any, Config]:
        nonlocal provider_called
        provider_called = True
        await asyncio.Event().wait()  # blocks forever
        raise AssertionError("unreachable")

    server = build_server(never_ready_provider)

    started = time.monotonic()
    tools = await asyncio.wait_for(_invoke_list_tools(server), timeout=_HANDSHAKE_BUDGET_SECS)
    elapsed = time.monotonic() - started

    assert len(tools) == 22
    assert not provider_called, "list_tools must never touch the walletd provider"
    assert elapsed < _HANDSHAKE_BUDGET_SECS


async def test_handshake_non_blocking_with_unready_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end-ish: a real supervisor mid-bring-up, real managed provider.

    We start the background bring-up with a SLOW stub spawn (the ready
    event is never set within the test) and confirm list_tools still
    returns the full tool list immediately. This is the stub-backed proof
    that the handshake does not block on walletd, independent of any real
    binary.
    """
    from exfer_mcp import server as server_mod

    sup = WalletdSupervisor(_managed_cfg(tmp_path))

    # Patch the bring-up body to block forever (walletd "still booting").
    block = asyncio.Event()

    async def slow_bring_up(url: str) -> None:
        assert sup._ready_event is not None
        await block.wait()  # never set during this test → never ready
        sup._ready_event.set()

    monkeypatch.setattr(sup, "_bring_up", slow_bring_up)

    url = sup.start_background()
    assert url.startswith("http://127.0.0.1:")

    provider = server_mod._managed_provider(sup)
    server = build_server(provider)

    try:
        tools = await asyncio.wait_for(_invoke_list_tools(server), timeout=_HANDSHAKE_BUDGET_SECS)
        assert len(tools) == 22
        # The bring-up is genuinely still pending (not yet ready).
        assert sup._ready_event is not None and not sup._ready_event.is_set()
    finally:
        block.set()
        if sup._bringup_task is not None:
            await sup._bringup_task


# ---------------------------------------------------------------------------
# A tool call awaits ensure_ready() then proceeds
# ---------------------------------------------------------------------------


async def test_tool_call_awaits_ready_then_proceeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First tool call blocks until ready, then dispatches to the handler."""
    from exfer_mcp import server as server_mod

    sup = WalletdSupervisor(_managed_cfg(tmp_path))

    gate = asyncio.Event()

    async def gated_bring_up(url: str) -> None:
        assert sup._ready_event is not None
        await gate.wait()
        sup._token = "spend-token"
        sup._ready_event.set()

    monkeypatch.setattr(sup, "_bring_up", gated_bring_up)

    fake = _FakeClient()
    # Don't actually construct a real AsyncClient — hand back the fake.
    monkeypatch.setattr(server_mod, "AsyncClient", lambda url, token, **kw: fake, raising=True)

    sup.start_background()
    provider = server_mod._managed_provider(sup)
    server = build_server(provider)

    # Kick the tool call off before readiness — it must be pending.
    call = asyncio.create_task(_invoke_call_tool(server, "exfer_get_block_height", {}))
    await asyncio.sleep(0.05)
    assert not call.done(), "tool call must await ensure_ready(), not complete early"
    assert fake.tip_calls == 0

    # Now let walletd become ready; the call should complete.
    gate.set()
    result = await asyncio.wait_for(call, timeout=2.0)
    assert result.isError is False
    assert fake.tip_calls == 1
    assert "4242" in result.content[0].text

    # Cached: a second call does NOT rebuild the client and returns at once.
    result2 = await asyncio.wait_for(
        _invoke_call_tool(server, "exfer_get_block_height", {}), timeout=2.0
    )
    assert result2.isError is False
    assert fake.tip_calls == 2

    if sup._bringup_task is not None:
        await sup._bringup_task


async def test_tool_call_errors_when_never_ready_within_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If bring-up fails, the tool call returns a clear error — never a hang."""
    from exfer_mcp import server as server_mod

    sup = WalletdSupervisor(_managed_cfg(tmp_path))

    async def failing_bring_up(url: str) -> None:
        assert sup._ready_event is not None
        sup._ready_error = ConfigError(
            "managed walletd did not become ready within 30s (last health probe: timed out)"
        )
        sup._ready_event.set()

    monkeypatch.setattr(sup, "_bring_up", failing_bring_up)
    # If a client were ever built it'd be a bug — make that loud.
    monkeypatch.setattr(
        server_mod,
        "AsyncClient",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("client built despite failure")),
        raising=True,
    )

    sup.start_background()
    provider = server_mod._managed_provider(sup)
    server = build_server(provider)

    result = await asyncio.wait_for(
        _invoke_call_tool(server, "exfer_get_block_height", {}), timeout=2.0
    )
    # The SDK turns the raised ConfigError into a structured tool error the
    # agent can read — it must NOT hang and must NOT crash the server.
    assert result.isError is True
    assert "did not become ready" in result.content[0].text

    if sup._bringup_task is not None:
        await sup._bringup_task


# ---------------------------------------------------------------------------
# ensure_ready() resolves once, idempotently, and returns (url, token)
# ---------------------------------------------------------------------------


async def test_ensure_ready_resolves_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sup = WalletdSupervisor(_managed_cfg(tmp_path))

    async def quick_bring_up(url: str) -> None:
        assert sup._ready_event is not None
        sup._token = "the-token"
        sup._ready_event.set()

    monkeypatch.setattr(sup, "_bring_up", quick_bring_up)
    url = sup.start_background()

    u1, t1 = await asyncio.wait_for(sup.ensure_ready(), timeout=2.0)
    # Idempotent: a second await returns immediately with the same values.
    u2, t2 = await asyncio.wait_for(sup.ensure_ready(), timeout=0.5)
    assert (u1, t1) == (url, "the-token") == (u2, t2)

    if sup._bringup_task is not None:
        await sup._bringup_task


async def test_start_background_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repeat start_background() must not spawn a second bring-up task."""
    sup = WalletdSupervisor(_managed_cfg(tmp_path))
    calls = 0

    async def counting_bring_up(url: str) -> None:
        nonlocal calls
        calls += 1
        assert sup._ready_event is not None
        sup._token = "tok"
        sup._ready_event.set()

    monkeypatch.setattr(sup, "_bring_up", counting_bring_up)
    url1 = sup.start_background()
    url2 = sup.start_background()
    assert url1 == url2
    await asyncio.wait_for(sup.ensure_ready(), timeout=2.0)
    assert calls == 1

    if sup._bringup_task is not None:
        await sup._bringup_task


# ---------------------------------------------------------------------------
# Cleanup: server shuts down before ready → the spawning walletd is killed
# ---------------------------------------------------------------------------


def test_stop_before_spawn_blocks_bringup(tmp_path: Path) -> None:
    """stop() before the background spawn lands → bring-up aborts, no spawn.

    Simulates the server exiting before walletd ever became ready: the
    blocking bring-up body must observe the teardown and refuse to spawn,
    so no orphan is created.
    """
    sup = WalletdSupervisor(_managed_cfg(tmp_path))
    sup.stop()  # teardown wins the race before bring-up runs
    with pytest.raises(ConfigError, match="shutting down"):
        sup._bring_up_blocking("http://127.0.0.1:7448")
    assert sup._proc is None  # nothing spawned


def test_stop_kills_spawned_child_even_if_never_ready(tmp_path: Path) -> None:
    """A child that spawned but never became ready is still torn down.

    Mirrors: background spawn succeeded, but the server exits (stop()) before
    readiness. The recorded child must be terminated — no orphan — and stop()
    stays idempotent.
    """
    import subprocess

    sup = WalletdSupervisor(_managed_cfg(tmp_path))
    proc = subprocess.Popen(["sleep", "30"])
    # Pretend the background bring-up spawned this and is mid-health-poll.
    sup._proc = proc
    sup._bringup_started = True

    sup.stop()
    assert proc.poll() is not None  # killed — no orphan
    code = proc.returncode
    sup.stop()  # idempotent
    assert proc.returncode == code


def test_spawn_after_stop_is_terminated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If stop() lands between the _stopped check and the actual Popen, the
    freshly-spawned child is killed immediately and bring-up aborts."""
    import subprocess

    sup = WalletdSupervisor(_managed_cfg(tmp_path))

    real_popen = subprocess.Popen
    spawned: list[subprocess.Popen[bytes]] = []

    def fake_popen(argv: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        # Race: teardown happens the instant before we publish the handle.
        proc = real_popen(["sleep", "30"])
        spawned.append(proc)
        sup._stopped = True  # stop() effectively already ran
        return proc

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    with pytest.raises(ConfigError, match="shutting down"):
        sup._spawn(sup.choose_bind().removeprefix("http://"))

    assert spawned, "Popen should have been called"
    assert spawned[0].poll() is not None, "the raced-in child must be terminated (no orphan)"


def test_startup_exit_explains_passphrase_mismatch(tmp_path: Path) -> None:
    # A second session whose passphrase differs from the one that sealed the
    # shared datadir gets a clean "wallet unavailable", not a bare exit code.
    sup = WalletdSupervisor(_managed_cfg(tmp_path))
    sup._log_tail.append("Error: keystore locked: decryption failed (wrong passphrase?)")
    msg = sup._explain_startup_exit(1)
    assert "managed wallet unavailable" in msg
    assert "passphrase" in msg.lower()
    assert "WALLETD_DATADIR" in msg and "external mode" in msg


def test_startup_exit_explains_in_use(tmp_path: Path) -> None:
    # A concurrent second walletd on the same datadir (DB lock) is reported as
    # already-in-use rather than a cryptic crash.
    sup = WalletdSupervisor(_managed_cfg(tmp_path))
    sup._log_tail.append("error: database is locked by another process")
    msg = sup._explain_startup_exit(1)
    assert "already in use by another exfer-mcp session" in msg
