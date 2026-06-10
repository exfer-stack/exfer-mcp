"""Tests for the zero-setup walletd auto-download (mocked HTTP)."""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path

import pytest

from exfer_mcp import walletd_fetch as wf
from exfer_mcp.config import ConfigError

_BLOB = b"#!/bin/sh\necho fake-walletd\n"
_KNOWN_ASSETS = {
    "exfer-walletd-linux-x86_64",
    "exfer-walletd-linux-arm64",
    "exfer-walletd-macos-x86_64",
    "exfer-walletd-macos-arm64",
    "exfer-walletd-windows-x86_64.exe",
}


@pytest.fixture
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("EXFER_WALLETD_VERSION", "v9.9.9-test")


def _sums(asset: str, blob: bytes = _BLOB) -> bytes:
    return f"{hashlib.sha256(blob).hexdigest()}  {asset}\n".encode()


def test_asset_name_is_a_published_asset() -> None:
    assert wf._asset_name() in _KNOWN_ASSETS


def test_fetch_verifies_caches_and_is_executable(
    isolated_cache: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset = wf._asset_name()

    def fake_get(url: str) -> bytes:
        return _sums(asset) if url.endswith("SHA256SUMS") else _BLOB

    monkeypatch.setattr(wf, "_http_get", fake_get)
    path = wf.ensure_walletd_binary()
    assert path.exists()
    assert path.read_bytes() == _BLOB
    assert path.stat().st_mode & stat.S_IXUSR, "downloaded binary must be executable"

    # Cache hit: a second call must NOT touch the network.
    def boom(url: str) -> bytes:
        raise AssertionError("cache hit must not re-download")

    monkeypatch.setattr(wf, "_http_get", boom)
    assert wf.ensure_walletd_binary() == path


def test_fetch_rejects_checksum_mismatch(
    isolated_cache: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset = wf._asset_name()

    def fake_get(url: str) -> bytes:
        if url.endswith("SHA256SUMS"):
            return f"{'0' * 64}  {asset}\n".encode()  # wrong hash
        return _BLOB

    monkeypatch.setattr(wf, "_http_get", fake_get)
    with pytest.raises(ConfigError, match="refusing to run an unverified"):
        wf.ensure_walletd_binary()


def test_fetch_refuses_release_without_checksums(
    isolated_cache: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_get(url: str) -> bytes:
        if url.endswith("SHA256SUMS"):
            raise OSError("404 Not Found")
        return _BLOB

    monkeypatch.setattr(wf, "_http_get", fake_get)
    with pytest.raises(ConfigError, match="SHA256SUMS"):
        wf.ensure_walletd_binary()
