"""Fetch + verify a prebuilt ``exfer-walletd`` for zero-setup managed mode.

When the operator provides no walletd (``EXFER_WALLETD_BIN`` unset and
``exfer-walletd`` not on ``PATH``), managed mode downloads the prebuilt binary
for this platform from the pinned ``exfer-walletd`` GitHub release, verifies it
against a SHA-256 **baked into this exfer-mcp release**, caches it, and returns
the path — the way Playwright fetches its browser.

Trust model — deliberately NOT the release's own ``SHA256SUMS``: that file lives
in the same release as the binary, so whoever can alter the release (a
compromised org/token, a mutable-asset swap, a TLS MITM) controls both and the
check would be theater. Instead the expected digests are pinned in
:data:`_PINNED_SHA256` here. The only thing that can change them is a new
exfer-mcp release, published to PyPI via Trusted Publishing (OIDC, no long-lived
token) — that provenance is the independent trust anchor. The binary is a
key-holding daemon that can spend funds, so it is executed ONLY after its bytes match the baked
digest, the cached copy is re-verified on EVERY run (never trusted blindly), and
the cache is created ``0o700``. An unpinned version, or any mismatch, is refused;
``EXFER_WALLETD_BIN`` always lets the operator supply a binary they built/trust.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import platform
import sys
import urllib.request
from pathlib import Path

from .config import ConfigError

WALLETD_REPO = "exfer-stack/exfer-walletd"

# Pinned known-good walletd release. Override with ``EXFER_WALLETD_VERSION`` (it
# must also appear in _PINNED_SHA256). v1.14.0 is the first release carrying both
# SHA256SUMS and the RPCs this MCP needs (EXFER-QUOTE, HTLC indexer delegation).
DEFAULT_WALLETD_VERSION = "v1.14.0"

# Independently computed + cross-checked against the release SHA256SUMS, then
# frozen here so the running code never trusts a co-located, mutable checksum.
# To support a new walletd build: download its 5 assets, sha256 them, add a
# version entry, and cut a new exfer-mcp release.
_PINNED_SHA256: dict[str, dict[str, str]] = {
    "v1.14.0": {
        "exfer-walletd-linux-x86_64": "6e049ddd8c13b7a3a36de25bd77af43f38cf126c9fc28503f95e8cad2bf16837",
        "exfer-walletd-linux-arm64": "619a7c04d3b67508e9bc357fa9336dadb2640008dd5e0244b27cb301105b834b",
        "exfer-walletd-macos-x86_64": "82279d4abd896473599b69552ab842c831f860c00eaa696661c956adb4b0e7b4",
        "exfer-walletd-macos-arm64": "7db4d2b6256b4c44b1c41c7df49777d047c812539be39e2048f131f32bfa3384",
        "exfer-walletd-windows-x86_64.exe": "c2d7718cc33cc642b01f937eb86696223887b6cee27a06f83c56408749f8ed1c",
    },
}

_DOWNLOAD_TIMEOUT_SECS = 180
_HASH_CHUNK = 1 << 20


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


def _harden(path: Path) -> None:
    """Best-effort: make a dir/file accessible to the owner only (0o700)."""
    with contextlib.suppress(OSError, NotImplementedError):
        path.chmod(0o700)


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "exfer-mcp"})
    with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT_SECS) as resp:
        data: bytes = resp.read()
    return data


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_HASH_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _expected_sha256(version: str, asset: str) -> str:
    """Return the SHA-256 baked into this exfer-mcp release for ``asset``.

    Refuses (no network fallback) if the version/asset isn't pinned — running an
    unverifiable key-holding binary is never an automatic behaviour.
    """
    try:
        return _PINNED_SHA256[version][asset]
    except KeyError:
        pinned = ", ".join(sorted(_PINNED_SHA256)) or "(none)"
        raise ConfigError(
            f"managed mode: this exfer-mcp release pins no verified SHA-256 for "
            f"walletd {version} / {asset}. It only runs a binary whose hash is baked "
            f"into exfer-mcp itself (the trust anchor). Pin EXFER_WALLETD_VERSION to a "
            f"baked version ({pinned}), upgrade exfer-mcp, or set EXFER_WALLETD_BIN to a "
            f"binary you built/trust."
        ) from None


def ensure_walletd_binary() -> Path:
    """Return a cached, digest-verified ``exfer-walletd`` for this platform.

    Verifies against a baked-in digest on first download AND re-verifies the
    cached copy on every call. Raises :class:`~.config.ConfigError` if the
    platform/version isn't pinned, the download fails, or bytes don't match.
    """
    version = os.environ.get("EXFER_WALLETD_VERSION") or DEFAULT_WALLETD_VERSION
    asset = _asset_name()
    expected = _expected_sha256(version, asset)  # baked, no network; raises if unpinned
    cache = _cache_dir(version)
    suffix = ".exe" if asset.endswith(".exe") else ""
    dest = cache / f"exfer-walletd{suffix}"

    # Never trust a cached key-holding binary blindly — re-hash it every run, so a
    # binary tampered with on disk after caching is caught and re-fetched.
    if dest.exists():
        if _sha256_file(dest) == expected:
            return dest
        _eprint("[walletd] cached binary failed re-verification — re-downloading")
        with contextlib.suppress(OSError):
            dest.unlink()

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
            f"managed mode: downloaded exfer-walletd {asset} has sha256 {actual}, but "
            f"exfer-mcp pins {expected} — refusing to run an unverified key-holding binary. "
            "Set EXFER_WALLETD_BIN to a binary you trust."
        )

    cache.mkdir(parents=True, exist_ok=True)
    _harden(cache)
    _harden(cache.parent)  # .../exfer-mcp/walletd
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(blob)
    tmp.chmod(0o700)  # owner rwx only — not group/other
    tmp.replace(dest)  # atomic publish, so a torn download is never used
    _eprint(f"[walletd] zero-setup: verified + cached exfer-walletd at {dest}")
    return dest
