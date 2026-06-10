"""render_error should make node-unreachable failures actionable."""

from __future__ import annotations

from exfer_mcp.tools._common import _looks_node_unreachable, render_error


def test_node_hint_added_on_upstream_failure() -> None:
    out = render_error(RuntimeError("HTTP 502 from walletd"))[0].text
    assert "EXFER_NODE_RPC" in out, "a 502/upstream error must point at EXFER_NODE_RPC"


def test_no_node_hint_on_ordinary_error() -> None:
    out = render_error(RuntimeError("bad params: foo"))[0].text
    assert "EXFER_NODE_RPC" not in out


def test_looks_node_unreachable_classifier() -> None:
    assert _looks_node_unreachable(RuntimeError("connection refused"))
    assert _looks_node_unreachable(RuntimeError("upstream timed out"))
    assert not _looks_node_unreachable(RuntimeError("insufficient balance"))
