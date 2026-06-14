"""Unit tests for the self-funding `earn` tools.

Pure/offline: they exercise registration and the idle/no-binary code paths
without spawning a real miner or touching the network.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from exfer_mcp.tools import HANDLERS, NO_WALLETD_TOOLS, TOOLS
from exfer_mcp.tools.earn import earn, earn_status, earn_stop

_NAMES = ("exfer_earn", "exfer_earn_probe", "exfer_earn_status", "exfer_earn_stop")


def test_earn_tools_registered() -> None:
    listed = {t.name for t in TOOLS}
    for name in _NAMES:
        assert name in listed, f"{name} missing from TOOLS"
        assert name in HANDLERS, f"{name} missing from HANDLERS"
        # Mining is independent of walletd, so they all skip the readiness gate.
        assert name in NO_WALLETD_TOOLS, f"{name} should be a no-walletd tool"


def test_earn_status_idle() -> None:
    out = asyncio.run(earn_status(None, {}, None))  # type: ignore[arg-type]
    data = json.loads(out[0].text)
    assert data["running"] is False
    assert data["stats"] == {}


def test_earn_stop_when_idle() -> None:
    out = asyncio.run(earn_stop(None, {}, None))  # type: ignore[arg-type]
    assert json.loads(out[0].text)["status"] == "not_running"


def test_earn_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the resolver to find nothing: no env, nothing on PATH.
    monkeypatch.delenv("EXFER_AGENT_MINER_BIN", raising=False)
    monkeypatch.setattr("shutil.which", lambda _name: None)
    out = asyncio.run(
        earn(None, {"address": "a" * 64, "miner_bin": "/nonexistent/exfer-agent-miner"}, None)  # type: ignore[arg-type]
    )
    assert "not found" in out[0].text.lower()
