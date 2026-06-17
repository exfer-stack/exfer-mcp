"""Supervisor unit tests.

Two layers:

* Pure-Python behaviour that needs no walletd: free-port selection,
  keystore-presence detection, mnemonic extraction, cleanup idempotency.
* A real spawn/health/teardown test gated on the actual walletd binary.
  It skips cleanly when the binary is absent so CI without it still
  passes; locally it spawns walletd against the default public node,
  health-checks it, and asserts teardown leaves no process behind.
"""

from __future__ import annotations

import dataclasses
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

from exfer_mcp.config import ManagedConfig
from exfer_mcp.walletd_supervisor import (
    WalletdSupervisor,
    _redact_secrets,
    _seeded_keystore_exists,
    find_free_loopback_port,
)

WALLETD_BIN = Path(
    os.environ.get(
        "EXFER_WALLETD_BIN",
        str(Path(__file__).resolve().parents[2] / "exfer-walletd/target/release/exfer-walletd"),
    )
)
DEFAULT_NODE = "http://64.176.231.198:9334,http://89.127.232.155:9334"


# -- port selection ------------------------------------------------------


def test_free_port_returns_preferred_when_free() -> None:
    # Grab a port the OS just handed us, release it, then claim it's free.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        free = int(s.getsockname()[1])
    assert find_free_loopback_port(free) == free


def test_free_port_falls_back_when_busy() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        occupied.bind(("127.0.0.1", 0))
        busy_port = int(occupied.getsockname()[1])
        occupied.listen(1)
        chosen = find_free_loopback_port(busy_port)
        assert chosen != busy_port
        # The chosen port must itself be bindable.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", chosen))


# -- keystore detection --------------------------------------------------


def test_keystore_absent(tmp_path: Path) -> None:
    assert _seeded_keystore_exists(tmp_path) is False


def test_keystore_present_seed(tmp_path: Path) -> None:
    (tmp_path / "wallets").mkdir()
    (tmp_path / "wallets" / "seed.enc").write_text("x")
    assert _seeded_keystore_exists(tmp_path) is True


def test_state_json_alone_is_not_seeded(tmp_path: Path) -> None:
    # A seedless keyring (state.json, no seed.enc) must NOT count as seeded —
    # otherwise managed init is skipped and the wallet has no recovery phrase.
    (tmp_path / "wallets").mkdir()
    (tmp_path / "wallets" / "state.json").write_text("{}")
    assert _seeded_keystore_exists(tmp_path) is False


# -- mnemonic extraction -------------------------------------------------


def test_extract_mnemonic_from_json() -> None:
    stdout = '{"exfer_address":"ab","mnemonic":"word1 word2 word3","bsc_address":"0x"}\n'
    assert WalletdSupervisor._extract_mnemonic(stdout) == "word1 word2 word3"


def test_extract_mnemonic_missing_returns_none() -> None:
    assert WalletdSupervisor._extract_mnemonic("no json here") is None


# -- log redaction (first-run token leak) --------------------------------

# A real spend-scope token shape: 64 lowercase hex chars (the one observed
# leaking in a managed spawn was 46d61ffa…f8e5a4).
_SPEND_TOKEN = "46d61ffa8b70f0ca8c974c0bc8c246229ec02c2968386240e9d5df1980f8e5a4"


def test_redact_masks_bare_64hex_token() -> None:
    line = f"  {_SPEND_TOKEN}"
    out = _redact_secrets(line)
    assert _SPEND_TOKEN not in out
    assert "REDACTED" in out


def test_redact_masks_token_in_banner_line() -> None:
    # The exact shape walletd prints on first-run init.
    line = f"spend token: {_SPEND_TOKEN}"
    out = _redact_secrets(line)
    assert _SPEND_TOKEN not in out


def test_redact_masks_all_three_scoped_tokens() -> None:
    read = "a" * 64
    manage = "b" * 64
    spend = "c" * 64
    line = f"read={read} manage={manage} spend={spend}"
    out = _redact_secrets(line)
    for tok in (read, manage, spend):
        assert tok not in out
    assert out.count("REDACTED") == 3


def test_redact_leaves_non_token_text_untouched() -> None:
    line = "[walletd] follower synced height=12345 peers=3"
    assert _redact_secrets(line) == line


def test_redact_does_not_clip_longer_hex_runs() -> None:
    # A 65-hex run isn't a token; the boundary guard means we don't mask a
    # partial substring of it (we only mask exact 64-hex runs).
    long_hex = "d" * 65
    out = _redact_secrets(long_hex)
    assert "REDACTED" not in out


# -- cleanup idempotency -------------------------------------------------


def _managed_cfg(tmp_path: Path, port: int = 7448) -> ManagedConfig:
    return ManagedConfig(
        binary=str(WALLETD_BIN),
        keystore_passphrase="throwaway-test-passphrase",
        node_rpc=DEFAULT_NODE,
        indexer_rpc=None,
        datadir=tmp_path / "dd",
        bind_host="127.0.0.1",
        bind_port=port,
    )


def test_build_argv_forwards_expect_genesis(tmp_path: Path) -> None:
    cfg = dataclasses.replace(_managed_cfg(tmp_path), expect_genesis="ab" * 32)
    argv = WalletdSupervisor(cfg)._build_argv("127.0.0.1:7448")
    i = argv.index("--expect-genesis")
    assert argv[i + 1] == "ab" * 32


def test_build_argv_omits_expect_genesis_by_default(tmp_path: Path) -> None:
    argv = WalletdSupervisor(_managed_cfg(tmp_path))._build_argv("127.0.0.1:7448")
    assert "--expect-genesis" not in argv


def test_child_env_forwards_swap_pool(tmp_path: Path) -> None:
    cfg = dataclasses.replace(
        _managed_cfg(tmp_path),
        swap_pool_url="https://pool.example:8080",
        swap_pool_ca="-----BEGIN CERTIFICATE-----\nXX\n-----END CERTIFICATE-----",
    )
    env = WalletdSupervisor(cfg)._child_env()
    assert env["WALLETD_SWAP_POOL"] == "https://pool.example:8080"
    assert "XX" in env["WALLETD_SWAP_POOL_CA"]


def test_child_env_omits_swap_pool_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WALLETD_SWAP_POOL", raising=False)
    monkeypatch.delenv("WALLETD_SWAP_POOL_CA", raising=False)
    env = WalletdSupervisor(_managed_cfg(tmp_path))._child_env()
    assert "WALLETD_SWAP_POOL" not in env
    assert "WALLETD_SWAP_POOL_CA" not in env


def test_stop_is_safe_with_no_process(tmp_path: Path) -> None:
    sup = WalletdSupervisor(_managed_cfg(tmp_path))
    # Never started → stop must be a no-op, and idempotent.
    sup.stop()
    sup.stop()


def test_stop_idempotent_kills_once(tmp_path: Path) -> None:
    sup = WalletdSupervisor(_managed_cfg(tmp_path))
    # Attach a long-lived dummy child (sleep) standing in for walletd.
    proc = subprocess.Popen(["sleep", "30"])
    sup._proc = proc
    sup.stop()
    assert proc.poll() is not None  # terminated
    first_code = proc.returncode
    # Second stop must not raise or wait on a reaped process.
    sup.stop()
    assert proc.returncode == first_code


# -- real spawn / health / teardown (gated on the binary) ----------------

_BINARY_PRESENT = WALLETD_BIN.exists() and os.access(WALLETD_BIN, os.X_OK)


@pytest.mark.skipif(not _BINARY_PRESENT, reason="exfer-walletd binary not present")
def test_real_spawn_health_and_teardown(tmp_path: Path) -> None:
    """Spawn the real walletd, health-check it, confirm teardown kills it."""
    # Use 0 → always pick a free ephemeral port so we never collide.
    cfg = _managed_cfg(tmp_path, port=find_free_loopback_port(7448))
    sup = WalletdSupervisor(cfg)
    try:
        url, token = sup.start()
        assert url.startswith("http://127.0.0.1:")
        assert token  # spend-scope bearer token
        # The keystore + scoped token files must now exist.
        assert (cfg.datadir / "wallets" / "seed.enc").exists()
        assert (cfg.datadir / "token-spend").exists()
        # start() only returns once its own get_block_height probe passed,
        # so reaching here already proves the spawned walletd was healthy.
        child = sup._proc
        assert child is not None
        child_pid = child.pid
        assert child.poll() is None  # alive
    finally:
        sup.stop()

    # No orphan: the recorded pid must be gone.
    assert child.poll() is not None
    _assert_pid_dead(child_pid)


def _assert_pid_dead(pid: int) -> None:
    """Poll until the pid is no longer a live process (or fail)."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return  # dead — good
        except PermissionError:
            return  # exists but not ours; close enough for this guard
        time.sleep(0.1)
    pytest.fail(f"walletd pid {pid} still alive after teardown (orphan!)")
