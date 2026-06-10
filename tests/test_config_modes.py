"""Mode-select + config-default tests for the EXTERNAL/MANAGED split.

These are pure-env tests — they never spawn walletd. They lock in the
defaults the README documents (public node + indexer, bind, datadir) and
the "WALLETD_URL set → EXTERNAL" decision.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exfer_mcp.config import (
    DEFAULT_BIND,
    DEFAULT_INDEXER_RPC,
    DEFAULT_NODE_RPC,
    Config,
    ConfigError,
    ManagedConfig,
    managed_mode_selected,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every env var the config layer reads so each test starts clean."""
    for var in (
        "WALLETD_URL",
        "WALLETD_AUTH_TOKEN",
        "WALLETD_FINGERPRINT",
        "WALLETD_KEYSTORE_PASSPHRASE",
        "EXFER_WALLETD_BIN",
        "EXFER_NODE_RPC",
        "EXFER_INDEXER_RPC",
        "WALLETD_DATADIR",
        "EXFER_WALLETD_BIND",
        "EXFER_MCP_DEFAULT_FEE_RATE",
        "EXFER_MCP_HTTPX_TIMEOUT",
    ):
        monkeypatch.delenv(var, raising=False)


# -- mode select ---------------------------------------------------------


def test_url_set_selects_external(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WALLETD_URL", "http://127.0.0.1:7448")
    assert managed_mode_selected() is False


def test_url_unset_selects_managed() -> None:
    assert managed_mode_selected() is True


def test_url_empty_selects_managed(monkeypatch: pytest.MonkeyPatch) -> None:
    # An explicit empty string is treated as "unset" → managed.
    monkeypatch.setenv("WALLETD_URL", "")
    assert managed_mode_selected() is True


# -- EXTERNAL config unchanged ------------------------------------------


def test_external_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WALLETD_URL", "http://127.0.0.1:7448")
    monkeypatch.setenv("WALLETD_AUTH_TOKEN", "tok")
    cfg = Config.from_env()
    assert cfg.walletd_url == "http://127.0.0.1:7448"
    assert cfg.walletd_token == "tok"
    assert cfg.walletd_fingerprint is None
    assert cfg.httpx_timeout == 30.0


def test_external_missing_token_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WALLETD_URL", "http://127.0.0.1:7448")
    with pytest.raises(ConfigError, match="WALLETD_AUTH_TOKEN"):
        Config.from_env()


# -- MANAGED defaults ----------------------------------------------------


def _bin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A fake on-disk binary so EXFER_WALLETD_BIN validation passes."""
    binary = tmp_path / "exfer-walletd"
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setenv("EXFER_WALLETD_BIN", str(binary))
    return binary


def test_managed_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _bin(monkeypatch, tmp_path)
    monkeypatch.setenv("WALLETD_KEYSTORE_PASSPHRASE", "pw")
    cfg = ManagedConfig.from_env()

    assert cfg.node_rpc == DEFAULT_NODE_RPC
    # Default to a multi-node failover list (assert shape, not specific IPs, so
    # the test survives future node changes).
    assert cfg.node_rpc.count("http://") >= 2, "default should list multiple nodes for failover"
    assert "9334" in cfg.node_rpc
    assert cfg.indexer_rpc == DEFAULT_INDEXER_RPC
    assert cfg.indexer_rpc is not None and "9335" in cfg.indexer_rpc
    assert cfg.bind_host == "127.0.0.1"
    assert f"{cfg.bind_host}:{cfg.bind_port}" == DEFAULT_BIND
    assert cfg.datadir == Path.home() / ".exfer-walletd-mcp"
    assert cfg.keystore_passphrase == "pw"


def test_managed_requires_passphrase(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _bin(monkeypatch, tmp_path)
    with pytest.raises(ConfigError, match="WALLETD_KEYSTORE_PASSPHRASE"):
        ManagedConfig.from_env()


def test_managed_binary_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WALLETD_KEYSTORE_PASSPHRASE", "pw")
    monkeypatch.setenv("EXFER_WALLETD_BIN", "/nonexistent/exfer-walletd")
    with pytest.raises(ConfigError, match="does not exist"):
        ManagedConfig.from_env()


def test_managed_autodetect_on_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # No EXFER_WALLETD_BIN → must find `exfer-walletd` on PATH.
    binary = tmp_path / "exfer-walletd"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("WALLETD_KEYSTORE_PASSPHRASE", "pw")
    cfg = ManagedConfig.from_env()
    assert cfg.binary == str(binary)


def test_managed_autodetect_defers_autodownload_off_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # No EXFER_WALLETD_BIN and nothing on PATH → from_env() must NOT download the
    # walletd binary at config time: a ~32MB fetch on the startup path blocks the
    # MCP handshake and trips host startup timeouts. It records binary=None; the
    # supervisor downloads lazily in its background bring-up instead.
    monkeypatch.delenv("EXFER_WALLETD_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))  # empty dir, no binary
    monkeypatch.setenv("WALLETD_KEYSTORE_PASSPHRASE", "pw")
    import exfer_mcp.walletd_fetch as wf

    calls = {"n": 0}

    def _should_not_run() -> Path:
        calls["n"] += 1
        return tmp_path / "downloaded-walletd"

    monkeypatch.setattr(wf, "ensure_walletd_binary", _should_not_run)
    cfg = ManagedConfig.from_env()
    assert cfg.binary is None, "auto-download must be deferred off the startup path"
    assert calls["n"] == 0, "from_env() must not trigger the walletd download"


def test_managed_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _bin(monkeypatch, tmp_path)
    monkeypatch.setenv("WALLETD_KEYSTORE_PASSPHRASE", "pw")
    monkeypatch.setenv("EXFER_NODE_RPC", "http://node.local:9334")
    monkeypatch.setenv("EXFER_INDEXER_RPC", "http://idx.local:9335")
    monkeypatch.setenv("WALLETD_DATADIR", str(tmp_path / "dd"))
    monkeypatch.setenv("EXFER_WALLETD_BIND", "127.0.0.1:9999")
    cfg = ManagedConfig.from_env()
    assert cfg.node_rpc == "http://node.local:9334"
    assert cfg.indexer_rpc == "http://idx.local:9335"
    assert cfg.datadir == tmp_path / "dd"
    assert cfg.bind_port == 9999


def test_managed_empty_indexer_disables(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _bin(monkeypatch, tmp_path)
    monkeypatch.setenv("WALLETD_KEYSTORE_PASSPHRASE", "pw")
    monkeypatch.setenv("EXFER_INDEXER_RPC", "")  # explicit disable
    cfg = ManagedConfig.from_env()
    assert cfg.indexer_rpc is None


def test_managed_bad_bind_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _bin(monkeypatch, tmp_path)
    monkeypatch.setenv("WALLETD_KEYSTORE_PASSPHRASE", "pw")
    monkeypatch.setenv("EXFER_WALLETD_BIND", "not-a-bind")
    with pytest.raises(ConfigError, match="host:port"):
        ManagedConfig.from_env()


# -- for_managed effective config ---------------------------------------


def test_for_managed_builds_effective_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXFER_MCP_HTTPX_TIMEOUT", "12")
    cfg = Config.for_managed("http://127.0.0.1:7448", "spend-token")
    assert cfg.walletd_url == "http://127.0.0.1:7448"
    assert cfg.walletd_token == "spend-token"
    assert cfg.walletd_fingerprint is None
    assert cfg.httpx_timeout == 12.0
