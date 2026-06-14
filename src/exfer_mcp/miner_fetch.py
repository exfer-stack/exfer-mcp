"""Fetch + verify a prebuilt ``exfer-agent-miner`` for zero-setup mining.

When the agent calls ``exfer_earn`` and no miner is provided (``EXFER_AGENT_MINER_BIN``
unset, ``exfer-agent-miner`` not on ``PATH``), this downloads the prebuilt CPU
miner for the platform from the pinned ``exfer-agent-miner`` GitHub release,
verifies it against a SHA-256 **baked into this exfer-mcp release**, caches it,
and returns the path — the same trust model as :mod:`walletd_fetch`.

Why baked digests, not the release's own ``SHA256SUMS``: that file ships in the
same release as the binary, so whoever can alter the release controls both. The
expected digests live in :data:`_PINNED_SHA256` here; the only thing that can
change them is a new exfer-mcp release published to PyPI via Trusted Publishing
(OIDC) — that provenance is the trust anchor. A miner that mines to the agent's
address is integrity-sensitive (a tampered build could redirect the payout), so
it runs ONLY after its bytes match the baked digest, the cached copy is
re-verified every run, and ``EXFER_AGENT_MINER_BIN`` always lets an operator
supply a binary they built (e.g. a GPU ``--features cuda`` build).

The published binary is CPU-only with zero CUDA dependency; GPU mining is opt-in
via a self-built ``--features cuda`` binary pointed at by ``EXFER_AGENT_MINER_BIN``.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import platform
import sys
import urllib.request
from pathlib import Path

MINER_REPO = "exfer-stack/exfer-agent-miner"

# Pinned known-good miner release. Override with ``EXFER_AGENT_MINER_VERSION``
# (it must also appear in _PINNED_SHA256).
DEFAULT_MINER_VERSION = "v0.1.0"

# Independently computed from the release assets, then frozen here so the running
# code never trusts a co-located, mutable checksum. To support a new miner build:
# download its assets, sha256 them, add a version entry, cut a new exfer-mcp release.
_PINNED_SHA256: dict[str, dict[str, str]] = {
    "v0.1.0": {
        "exfer-agent-miner-linux-x86_64": "2a6493b0177e5005dab344f1615fefd350c8d0c3a3896d1170cb05b83c9dd059",
        "exfer-agent-miner-linux-arm64": "22087f06e5b857d0fc7f62efeda5f991087a3aa4692b2234c07ac847b49a514a",
        "exfer-agent-miner-macos-x86_64": "03d74c5208505a2a991f596bb10226fe586fe15fb0a77839f20c470472f82033",
        "exfer-agent-miner-macos-arm64": "a36913b9822c4dee5c6abcea29ec96d32c262aa1c7a039d9859e4c04f40eb86f",
        "exfer-agent-miner-windows-x86_64.exe": "a68cf50d553553e5e5daa19625dec98a63ffe62fa034a76f17471dc4dffe8688",
    },
}

_DOWNLOAD_TIMEOUT_SECS = 180
_HASH_CHUNK = 1 << 20


class MinerFetchError(RuntimeError):
    """The miner could not be fetched/verified; the caller degrades gracefully."""


def _eprint(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _asset_name() -> str:
    osname = sys.platform
    machine = platform.machine().lower()
    if osname.startswith("win"):
        return "exfer-agent-miner-windows-x86_64.exe"
    if osname.startswith("linux"):
        ospart = "linux"
    elif osname == "darwin":
        ospart = "macos"
    else:
        raise MinerFetchError(
            f"no prebuilt exfer-agent-miner for platform {osname!r}. "
            "Set EXFER_AGENT_MINER_BIN to a binary you built."
        )
    if machine in ("x86_64", "amd64"):
        arch = "x86_64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        raise MinerFetchError(
            f"no prebuilt exfer-agent-miner for architecture {machine!r}. "
            "Set EXFER_AGENT_MINER_BIN to a binary you built."
        )
    return f"exfer-agent-miner-{ospart}-{arch}"


def _cache_dir(version: str) -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "exfer-mcp" / "agent-miner" / version


def _harden(path: Path) -> None:
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
    try:
        sha = _PINNED_SHA256[version][asset]
    except KeyError:
        pinned = ", ".join(sorted(_PINNED_SHA256)) or "(none)"
        raise MinerFetchError(
            f"this exfer-mcp release pins no verified SHA-256 for miner {version} / {asset} "
            f"(pinned: {pinned}). Upgrade exfer-mcp, pin EXFER_AGENT_MINER_VERSION to a baked "
            f"version, or set EXFER_AGENT_MINER_BIN to a binary you built."
        ) from None
    if sha.startswith("__"):
        raise MinerFetchError(
            "this exfer-mcp build has a placeholder miner digest (unreleased). "
            "Set EXFER_AGENT_MINER_BIN to a binary you built."
        )
    return sha


def ensure_miner_binary() -> Path:
    """Return a cached, digest-verified ``exfer-agent-miner`` for this platform.

    Verifies against a baked-in digest on first download AND re-verifies the
    cached copy on every call. Raises :class:`MinerFetchError` if the
    platform/version isn't pinned, the download fails, or bytes don't match.
    """
    version = os.environ.get("EXFER_AGENT_MINER_VERSION") or DEFAULT_MINER_VERSION
    asset = _asset_name()
    expected = _expected_sha256(version, asset)
    cache = _cache_dir(version)
    suffix = ".exe" if asset.endswith(".exe") else ""
    dest = cache / f"exfer-agent-miner{suffix}"

    if dest.exists():
        if _sha256_file(dest) == expected:
            return dest
        _eprint("[miner] cached binary failed re-verification — re-downloading")
        with contextlib.suppress(OSError):
            dest.unlink()

    url = f"https://github.com/{MINER_REPO}/releases/download/{version}/{asset}"
    _eprint(f"[miner] zero-setup: downloading {asset} ({version})...")
    try:
        blob = _http_get(url)
    except Exception as exc:
        raise MinerFetchError(
            f"failed to download exfer-agent-miner {version} ({asset}): {exc}. "
            "Set EXFER_AGENT_MINER_BIN to a local binary, or check connectivity."
        ) from exc

    actual = hashlib.sha256(blob).hexdigest()
    if actual != expected:
        raise MinerFetchError(
            f"downloaded exfer-agent-miner {asset} has sha256 {actual}, but exfer-mcp pins "
            f"{expected} — refusing to run an unverified miner. Set EXFER_AGENT_MINER_BIN to a "
            "binary you trust."
        )

    cache.mkdir(parents=True, exist_ok=True)
    _harden(cache)
    _harden(cache.parent)
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(blob)
    tmp.chmod(0o700)
    tmp.replace(dest)
    _eprint(f"[miner] zero-setup: verified + cached exfer-agent-miner at {dest}")
    return dest
