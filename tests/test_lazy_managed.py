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
import os
import sys
import time
from pathlib import Path
from typing import Any

import mcp.types as mcp_types
import pytest

from exfer_mcp.config import Config, ConfigError, ManagedConfig
from exfer_mcp.server import build_server
from exfer_mcp.walletd_supervisor import WalletdSupervisor, _seeded_keystore_exists

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

    assert len(tools) == 23
    assert not provider_called, "list_tools must never touch the walletd provider"
    assert elapsed < _HANDSHAKE_BUDGET_SECS


async def test_check_update_tool_bypasses_walletd_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    # exfer_check_update must work without (and not block on) walletd readiness —
    # otherwise you couldn't check for updates while walletd is down.
    monkeypatch.setenv("EXFER_MCP_NO_UPDATE_CHECK", "1")  # deterministic, no network
    provider_called = False

    async def never_ready_provider() -> tuple[Any, Config]:
        nonlocal provider_called
        provider_called = True
        await asyncio.Event().wait()  # blocks forever
        raise AssertionError("unreachable")

    server = build_server(never_ready_provider)
    result = await asyncio.wait_for(
        _invoke_call_tool(server, "exfer_check_update", {}), timeout=_HANDSHAKE_BUDGET_SECS
    )
    assert result.isError is False
    assert not provider_called, "update check must not gate on the walletd provider"


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
        assert len(tools) == 23
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


async def test_ensure_ready_retries_after_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A failed bring-up must not wedge the session: the next ensure_ready()
    # relaunches a fresh attempt (self-heal once the cause clears).
    sup = WalletdSupervisor(_managed_cfg(tmp_path))
    attempts = {"n": 0}

    async def flaky_bring_up(url: str) -> None:
        event = sup._ready_event
        assert event is not None
        attempts["n"] += 1
        if attempts["n"] == 1:
            sup._ready_error = ConfigError("first attempt fails (datadir locked)")
        else:
            sup._ready_error = None
            sup._token = "spend-token"
        event.set()

    monkeypatch.setattr(sup, "_bring_up", flaky_bring_up)
    sup.start_background()
    _url, token = await sup.ensure_ready()
    assert token == "spend-token"
    assert attempts["n"] == 2, "ensure_ready should relaunch exactly one retry"


async def test_ensure_ready_does_not_retry_permanent_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A permanent cause (wrong passphrase) must NOT respawn — fast-fail at once.
    sup = WalletdSupervisor(_managed_cfg(tmp_path))
    attempts = {"n": 0}

    async def perm_bring_up(url: str) -> None:
        event = sup._ready_event
        assert event is not None
        attempts["n"] += 1
        sup._ready_error = ConfigError("managed wallet unavailable: could not be unlocked (passphrase)")
        sup._failure_count += 1
        sup._last_failure_at = time.monotonic()
        event.set()

    monkeypatch.setattr(sup, "_bring_up", perm_bring_up)
    sup.start_background()
    with pytest.raises(ConfigError, match="could not be unlocked"):
        await sup.ensure_ready()
    assert attempts["n"] == 1, "a permanent (passphrase) error must not respawn"


async def test_ensure_ready_backs_off_after_repeated_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A transient-looking but persistent cause (node unreachable) gets one free
    # retry, then backs off — no respawn-storm on every tool call.
    sup = WalletdSupervisor(_managed_cfg(tmp_path))
    attempts = {"n": 0}

    async def fail_bring_up(url: str) -> None:
        event = sup._ready_event
        assert event is not None
        attempts["n"] += 1
        sup._ready_error = ConfigError("node unreachable")
        sup._failure_count += 1
        sup._last_failure_at = time.monotonic()
        event.set()

    monkeypatch.setattr(sup, "_bring_up", fail_bring_up)
    sup.start_background()
    with pytest.raises(ConfigError):
        await sup.ensure_ready()  # attempt 1 (free retry) -> attempt 2 -> raise
    assert attempts["n"] == 2
    with pytest.raises(ConfigError):
        await sup.ensure_ready()  # within backoff window: must NOT respawn
    assert attempts["n"] == 2, "within the backoff window there must be no new spawn"


class _FakeProc:
    """Minimal psutil.Process stand-in for reap tests."""

    def __init__(self, pid: int, cmdline: list[str], ppid: int = 1) -> None:
        self.pid = pid
        self.info = {"cmdline": cmdline, "ppid": ppid}
        self.killed = False

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        return 0


def test_reap_kills_orphan_but_spares_live_peer_and_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Reap ONLY a true orphan (parent gone) on OUR datadir — never a concurrent
    # live session's walletd, another datadir's, or a non-walletd process.
    import exfer_mcp.walletd_supervisor as mod

    sup = WalletdSupervisor(_managed_cfg(tmp_path))
    dd = str(sup._config.datadir)
    other_dd = _FakeProc(101, ["/x/exfer-walletd", "--datadir", "/some/other/dir"])
    not_walletd = _FakeProc(102, ["python", "-m", "http.server", dd])  # has dd, not walletd
    prefix_dd = _FakeProc(103, ["/x/exfer-walletd", "--datadir", dd + "-sibling"])  # prefix only
    live_peer = _FakeProc(104, ["/x/exfer-walletd", "--datadir", dd], ppid=os.getpid())  # alive parent
    orphan = _FakeProc(105, ["/x/exfer-walletd", "--datadir", dd], ppid=1)  # reparented to init
    monkeypatch.setattr(
        mod.psutil,
        "process_iter",
        lambda attrs=None: iter([other_dd, not_walletd, prefix_dd, live_peer, orphan]),
    )

    sup._reap_stale_walletd()

    assert not other_dd.killed, "must not touch a walletd on another datadir"
    assert not not_walletd.killed, "must not touch a non-walletd process"
    assert not prefix_dd.killed, "must match the datadir by realpath, not a prefix"
    assert not live_peer.killed, "must NOT kill a walletd owned by a live concurrent session"
    assert orphan.killed, "must kill a true orphan whose parent is gone"


def test_binary_path_downloads_lazily_when_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """config.binary=None (zero-setup, deferred) → _binary_path() fetches once.

    The download is driven from the bring-up path via _binary_path(), never from
    the handshake, and the result is cached so repeated spawns don't re-fetch.
    """
    import exfer_mcp.walletd_fetch as wf

    cfg = ManagedConfig(
        binary=None,  # not on PATH / no EXFER_WALLETD_BIN → deferred auto-download
        keystore_passphrase="pw",
        node_rpc="http://node.test:9334",
        indexer_rpc=None,
        datadir=tmp_path / "dd",
        bind_host="127.0.0.1",
        bind_port=7448,
    )
    sup = WalletdSupervisor(cfg)

    fake = tmp_path / "downloaded-walletd"
    fake.write_text("#!/bin/sh\n")
    calls = {"n": 0}

    def _fetch() -> Path:
        calls["n"] += 1
        return fake

    monkeypatch.setattr(wf, "ensure_walletd_binary", _fetch)

    assert sup._binary_path() == str(fake)
    assert sup._binary_path() == str(fake)  # cached — no second fetch
    assert calls["n"] == 1


def test_binary_path_returns_configured_binary_without_download(tmp_path: Path) -> None:
    # An explicit/PATH-resolved binary is returned as-is — no fetch attempted.
    sup = WalletdSupervisor(_managed_cfg(tmp_path))  # binary="/nonexistent/exfer-walletd"
    assert sup._binary_path() == "/nonexistent/exfer-walletd"


def test_prewarm_fetches_binary_then_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # `--prewarm` must fetch+verify the binary and NOT enter the async server loop.
    import exfer_mcp.walletd_fetch as wf
    from exfer_mcp import server as server_mod

    fake = tmp_path / "wd"
    fake.write_text("#!/bin/sh\n")
    calls = {"n": 0}

    def _fetch() -> Path:
        calls["n"] += 1
        return fake

    monkeypatch.setattr(wf, "ensure_walletd_binary", _fetch)
    monkeypatch.setattr(sys, "argv", ["exfer-mcp", "--prewarm"])
    monkeypatch.setattr(
        server_mod.asyncio,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not serve in --prewarm")),
    )

    server_mod.main()

    assert calls["n"] == 1
    assert "walletd binary ready" in capsys.readouterr().out


def test_prewarm_failure_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import exfer_mcp.walletd_fetch as wf
    from exfer_mcp import server as server_mod

    def _fail() -> Path:
        raise ConfigError("no prebuilt walletd for this platform")

    monkeypatch.setattr(wf, "ensure_walletd_binary", _fail)
    monkeypatch.setattr(sys, "argv", ["exfer-mcp", "--prewarm"])

    with pytest.raises(SystemExit) as exc_info:
        server_mod.main()
    assert exc_info.value.code == 1
    assert "pre-warm failed" in capsys.readouterr().err


def test_reap_noop_on_clean_datadir(tmp_path: Path) -> None:
    # Real psutil scan over the live process table: a fresh tmp datadir matches
    # nothing, so the reap is a harmless no-op (and never raises).
    sup = WalletdSupervisor(_managed_cfg(tmp_path))
    sup._reap_stale_walletd()


def test_seeded_keystore_exists_requires_seed_enc(tmp_path: Path) -> None:
    seeded = tmp_path / "a"
    (seeded / "wallets").mkdir(parents=True)
    (seeded / "wallets" / "seed.enc").write_bytes(b"x")
    assert _seeded_keystore_exists(seeded)

    seedless = tmp_path / "b"
    (seedless / "wallets").mkdir(parents=True)
    (seedless / "wallets" / "state.json").write_text("{}")  # seedless, no seed.enc
    assert not _seeded_keystore_exists(seedless), "state.json alone must not count as seeded"


def test_init_rejects_seedless_populated_datadir(tmp_path: Path) -> None:
    sup = WalletdSupervisor(_managed_cfg(tmp_path))
    (sup._config.datadir / "wallets").mkdir(parents=True)
    (sup._config.datadir / "wallets" / "state.json").write_text("{}")  # seedless owner
    with pytest.raises(ConfigError, match="seedless/foreign wallet"):
        sup._init_keystore_if_needed()  # must reject BEFORE spawning init-seeded


def test_announce_mnemonic_writes_0600_file_not_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sup = WalletdSupervisor(_managed_cfg(tmp_path))
    sup._config.datadir.mkdir(parents=True, exist_ok=True)
    phrase = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima"
    sup._announce_mnemonic(phrase, raw_stdout="{}")

    path = sup._config.datadir / "RECOVERY_PHRASE.txt"
    assert path.exists() and phrase in path.read_text()
    if hasattr(os, "getuid"):  # POSIX: confirm 0600
        assert (path.stat().st_mode & 0o777) == 0o600
    # The phrase MUST NOT be echoed to stderr (hosts persist that to logs).
    err = capsys.readouterr().err
    assert phrase not in err, "recovery phrase must not be written to the log channel"
    assert str(path) in err, "stderr should point the user at the saved file"
