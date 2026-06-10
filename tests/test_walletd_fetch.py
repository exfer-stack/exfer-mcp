"""Tests for the zero-setup walletd auto-download (baked-hash verification)."""

from __future__ import annotations

import hashlib
import re
import stat

import pytest

from exfer_mcp import walletd_fetch as wf
from exfer_mcp.config import ConfigError

_BLOB = b"#!/bin/sh\necho fake-walletd\n"
_VER = "v0.0.0-test"
_KNOWN_ASSETS = {
    "exfer-walletd-linux-x86_64",
    "exfer-walletd-linux-arm64",
    "exfer-walletd-macos-x86_64",
    "exfer-walletd-macos-arm64",
    "exfer-walletd-windows-x86_64.exe",
}


@pytest.fixture
def isolated(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("EXFER_WALLETD_VERSION", _VER)


def _pin(monkeypatch: pytest.MonkeyPatch, digest: str) -> str:
    """Pin `digest` for the current platform's asset under the test version."""
    asset = wf._asset_name()
    monkeypatch.setitem(wf._PINNED_SHA256, _VER, {asset: digest})
    return asset


def test_asset_name_is_a_published_asset() -> None:
    assert wf._asset_name() in _KNOWN_ASSETS


def test_shipped_default_version_is_pinned_for_all_assets() -> None:
    # The version exfer-mcp ships MUST have baked digests, or zero-setup is dead.
    assert wf.DEFAULT_WALLETD_VERSION in wf._PINNED_SHA256
    pins = wf._PINNED_SHA256[wf.DEFAULT_WALLETD_VERSION]
    assert set(pins) >= _KNOWN_ASSETS, "every platform asset must be pinned"
    for asset, digest in pins.items():
        assert re.fullmatch(r"[0-9a-f]{64}", digest), asset


def test_fetch_verifies_caches_and_reuses(isolated: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _pin(monkeypatch, hashlib.sha256(_BLOB).hexdigest())
    calls = {"n": 0}

    def fake_get(url: str) -> bytes:
        calls["n"] += 1
        return _BLOB

    monkeypatch.setattr(wf, "_http_get", fake_get)
    path = wf.ensure_walletd_binary()
    assert path.exists() and path.read_bytes() == _BLOB
    assert path.stat().st_mode & stat.S_IXUSR, "must be executable"
    assert calls["n"] == 1
    # Cache hit re-verifies from disk — no second download.
    assert wf.ensure_walletd_binary() == path and calls["n"] == 1


def test_tampered_cache_is_reverified_and_refetched(
    isolated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin(monkeypatch, hashlib.sha256(_BLOB).hexdigest())
    monkeypatch.setattr(wf, "_http_get", lambda url: _BLOB)
    path = wf.ensure_walletd_binary()
    path.write_bytes(b"TAMPERED-ON-DISK")  # corrupt the cached binary
    path2 = wf.ensure_walletd_binary()  # must detect mismatch and re-download
    assert path2.read_bytes() == _BLOB, "a tampered cached binary must never be trusted"


def test_fetch_rejects_checksum_mismatch(isolated: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _pin(monkeypatch, "0" * 64)  # wrong digest
    monkeypatch.setattr(wf, "_http_get", lambda url: _BLOB)
    with pytest.raises(ConfigError, match="refusing to run an unverified"):
        wf.ensure_walletd_binary()


def test_refuses_unpinned_version_without_touching_network(
    isolated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # _VER is deliberately NOT pinned. Must refuse BEFORE any download.
    def boom(url: str) -> bytes:
        raise AssertionError("must not hit the network for an unpinned version")

    monkeypatch.setattr(wf, "_http_get", boom)
    with pytest.raises(ConfigError, match="pins no verified SHA-256"):
        wf.ensure_walletd_binary()
