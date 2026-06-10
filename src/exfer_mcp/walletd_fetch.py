"""Fetch + verify a prebuilt ``exfer-walletd`` for zero-setup managed mode.

When the operator provides no walletd (``EXFER_WALLETD_BIN`` unset and
``exfer-walletd`` not on ``PATH``), managed mode downloads the prebuilt binary
for this platform from the pinned ``exfer-walletd`` GitHub release, verifies
its SHA-256 against that release's ``SHA256SUMS``, caches it under the user
cache dir, and returns the path — the way Playwright fetches its browser.

The binary is a hot-wallet daemon, so a download is executed ONLY after its
checksum matches the release's ``SHA256SUMS``. A release without ``SHA256SUMS``
(checksums were added in a later release) or a checksum mismatch is REFUSED —
the operator can always point ``EXFER_WALLETD_BIN`` at a binary they trust.
"""

from __future__ import annotations

import hashlib
import os
import platform
import stat
import sys
import urllib.request
from pathlib import Path

from .config import ConfigError

WALLETD_REPO = "exfer-stack/exfer-walletd"

# Pinned known-good walletd release. Override with ``EXFER_WALLETD_VERSION``.
# MUST be a release that publishes ``SHA256SUMS`` — auto-download refuses to run
# an unverifiable hot-wallet binary. v1.14.0 is the first checksummed release
# and carries the RPCs this MCP needs (EXFER-QUOTE, HTLC indexer delegation).
DEFAULT_WALLETD_VERSION = "v1.14.0"

_DOWNLOAD_TIMEOUT_SECS = 180


def _eprint(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _asset_name() -> str:
    """The ``exfer-walletd`` release asset for the current platform, or raise."""
    osname = sys.platform
    machine = platform.machine().lower()
    if osname.startswith("win"):
        # Only an x86_64 Windows binary is published.
        return "exfer-walletd-windows-x86_64.exe"
    if osname.startswith("linux"):
        ospart = "linux"
    elif osname == "darwin":
        ospart = "macos"
    else:
        raise ConfigError(
            f"managed mode: no prebuilt exfer-walletd for platform {osname!r}. "
            "Set EXFER_WALLETD_BIN to a walletd binary you built/trust."
        )
    if machine in ("x86_64", "amd64"):
        arch = "x86_64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        raise ConfigError(
            f"managed mode: no prebuilt exfer-walletd for architecture {machine!r}. "
            "Set EXFER_WALLETD_BIN to a walletd binary you built/trust."
        )
    return f"exfer-walletd-{ospart}-{arch}"


def _cache_dir(version: str) -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "exfer-mcp" / "walletd" / version


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "exfer-mcp"})
    with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT_SECS) as resp:
        data: bytes = resp.read()
    return data


def _expected_sha256(version: str, asset: str) -> str:
    """Return the SHA-256 hex for ``asset`` from the release's SHA256SUMS."""
    url = f"https://github.com/{WALLETD_REPO}/releases/download/{version}/SHA256SUMS"
    try:
        text = _http_get(url).decode("utf-8", "replace")
    except Exception as exc:
        raise ConfigError(
            f"managed mode: exfer-walletd release {version} publishes no SHA256SUMS, "
            "so the auto-downloaded binary can't be verified. Set EXFER_WALLETD_BIN to "
            "a walletd you trust, or pin EXFER_WALLETD_VERSION to a checksummed release."
        ) from exc
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == asset:
            return parts[0].lower()
    raise ConfigError(
        f"managed mode: SHA256SUMS for {version} has no entry for {asset!r} "
        "(unexpected release layout). Set EXFER_WALLETD_BIN instead."
    )


def ensure_walletd_binary() -> Path:
    """Return a cached, checksum-verified ``exfer-walletd`` for this platform.

    Downloads + verifies on first use, then reuses the cached copy. Raises
    :class:`~.config.ConfigError` if the platform has no prebuilt binary, the
    release has no checksums, or the download fails verification.
    """
    version = os.environ.get("EXFER_WALLETD_VERSION") or DEFAULT_WALLETD_VERSION
    asset = _asset_name()
    cache = _cache_dir(version)
    suffix = ".exe" if asset.endswith(".exe") else ""
    dest = cache / f"exfer-walletd{suffix}"
    if dest.exists():
        return dest

    expected = _expected_sha256(version, asset)
    url = f"https://github.com/{WALLETD_REPO}/releases/download/{version}/{asset}"
    _eprint(f"[walletd] zero-setup: downloading {asset} ({version})...")
    try:
        blob = _http_get(url)
    except Exception as exc:
        raise ConfigError(
            f"managed mode: failed to download exfer-walletd {version} ({asset}): {exc}. "
            "Set EXFER_WALLETD_BIN to a local binary, or check connectivity."
        ) from exc

    actual = hashlib.sha256(blob).hexdigest()
    if actual != expected:
        raise ConfigError(
            f"managed mode: downloaded exfer-walletd {asset} has sha256 {actual}, "
            f"expected {expected} from the release SHA256SUMS — refusing to run an "
            "unverified hot-wallet binary. Set EXFER_WALLETD_BIN to a binary you trust."
        )

    cache.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(blob)
    tmp.chmod(tmp.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    tmp.replace(dest)  # atomic publish, so a torn download is never used
    _eprint(f"[walletd] zero-setup: verified + cached exfer-walletd at {dest}")
    return dest
