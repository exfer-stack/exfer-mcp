"""Best-effort update check against PyPI — notify, never auto-apply.

For a tool that can spend a wallet, silently pulling new code or a new binary is
a supply-chain attack vector, so exfer-mcp **never updates itself**. This module
only *detects* whether a newer release exists (and whether the running version
was yanked — a security recall) and hands the operator the exact command to
update deliberately.

It is read-only with respect to the wallet: it touches only PyPI (network) and
its own small cache file under ``~/.cache/exfer-mcp``. Updating exfer-mcp swaps
the Python code and the (re-verified) walletd binary only — the wallet keystore
in ``WALLETD_DATADIR`` (``seed.enc``, ``RECOVERY_PHRASE.txt``, tokens, the index)
is never read, moved, or deleted by an update.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._version import __version__

_PYPI_JSON = "https://pypi.org/pypi/exfer-mcp/json"
_CHECK_INTERVAL_SECS = 24 * 3600
_HTTP_TIMEOUT_SECS = 4.0
_DISABLE_ENV = "EXFER_MCP_NO_UPDATE_CHECK"


@dataclass(frozen=True)
class UpdateInfo:
    current: str
    latest: str | None
    update_available: bool
    current_yanked: bool

    def how_to_update(self) -> str:
        target = self.latest or "<new-version>"
        return (
            f"exfer-mcp does not auto-update. To move to {target}, re-pin the version "
            f"in your MCP host and reload — Claude Code: re-add with `uvx exfer-mcp=={target}`; "
            f"config-file hosts (Claude Desktop / Cursor / Codex): set the arg to "
            f"`exfer-mcp=={target}`. (Unpinned `uvx exfer-mcp`: `uvx --refresh exfer-mcp`. "
            f"uv tool / pipx: `uv tool upgrade exfer-mcp` / `pipx upgrade exfer-mcp`.) "
            f"Updating does NOT touch your wallet data in WALLETD_DATADIR; the new walletd "
            f"binary is auto-downloaded and SHA-256-verified on next start."
        )


def _disabled() -> bool:
    return os.environ.get(_DISABLE_ENV, "").strip().lower() not in ("", "0", "false", "no")


def _cache_file() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "exfer-mcp" / "update-check.json"


def _parse_version(v: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", v or "")
    return tuple(int(p) for p in parts[:4]) or (0,)


def _is_newer(latest: str, current: str) -> bool:
    return _parse_version(latest) > _parse_version(current)


def _fetch_pypi() -> dict[str, Any] | None:
    req = urllib.request.Request(_PYPI_JSON, headers={"User-Agent": "exfer-mcp-update-check"})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECS) as resp:
            data: Any = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _read_cache() -> dict[str, Any] | None:
    try:
        data: Any = json.loads(_cache_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_cache(latest: str | None, current_yanked: bool) -> None:
    payload = {
        "checked_at": time.time(),
        "current": __version__,
        "latest": latest,
        "current_yanked": current_yanked,
    }
    try:
        path = _cache_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass


def _latest_from(data: dict[str, Any]) -> str | None:
    info = data.get("info")
    version = info.get("version") if isinstance(info, dict) else None
    return version if isinstance(version, str) else None


def _current_is_yanked(data: dict[str, Any], current: str) -> bool:
    releases = data.get("releases")
    files = releases.get(current) if isinstance(releases, dict) else None
    if not isinstance(files, list) or not files:
        return False
    return all(isinstance(f, dict) and f.get("yanked") for f in files)


def check_for_update(*, force: bool = False) -> UpdateInfo | None:
    """Detect a newer / yanked release. Returns None if disabled or undeterminable.

    Always fail-silent — a network error or unreadable cache yields None (or a
    stale cached result), never an exception. Cached for ~24h unless ``force``.
    """
    current = __version__
    if _disabled():
        return None

    cache = _read_cache()
    cached_latest: str | None = None
    cached_yanked = False
    cache_ok = False  # a cache entry exists for THIS running version
    fresh = False
    if cache is not None and cache.get("current") == current:
        cache_ok = True
        raw_latest = cache.get("latest")
        cached_latest = raw_latest if isinstance(raw_latest, str) else None
        cached_yanked = bool(cache.get("current_yanked"))
        age = time.time() - float(cache.get("checked_at", 0) or 0)
        fresh = (not force) and age < _CHECK_INTERVAL_SECS

    latest: str | None
    if fresh:
        latest, yanked = cached_latest, cached_yanked
    else:
        data = _fetch_pypi()
        if data is None:
            # Network miss: reuse a matching (possibly stale) cache, else give up.
            if not cache_ok:
                return None
            latest, yanked = cached_latest, cached_yanked
        else:
            latest = _latest_from(data)
            yanked = _current_is_yanked(data, current)
            _write_cache(latest, yanked)

    update_available = latest is not None and _is_newer(latest, current)
    return UpdateInfo(
        current=current,
        latest=latest,
        update_available=update_available,
        current_yanked=yanked,
    )


def startup_notice() -> str | None:
    """A one-line stderr notice if an update is available / the version is yanked.

    Best-effort + cached; returns None when nothing to say. Never raises.
    """
    try:
        info = check_for_update()
    except Exception:
        return None
    if info is None:
        return None
    if info.current_yanked:
        return (
            f"[exfer-mcp] SECURITY: your version {info.current} has been YANKED from PyPI "
            f"(recalled). Update now to {info.latest or 'the latest release'}."
        )
    if info.update_available:
        return f"[exfer-mcp] update available: {info.current} -> {info.latest} (you decide when; never auto-applied)."
    return None
