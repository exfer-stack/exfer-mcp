"""Tests for the PyPI update check (notify-only, fail-silent)."""

from __future__ import annotations

import pytest

from exfer_mcp import update_check as uc


@pytest.fixture
def isolated(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))  # isolate the cache file
    monkeypatch.delenv("EXFER_MCP_NO_UPDATE_CHECK", raising=False)


def test_version_compare() -> None:
    assert uc._is_newer("0.3.0", "0.2.2")
    assert uc._is_newer("0.2.10", "0.2.9")
    assert not uc._is_newer("0.2.2", "0.2.2")
    assert not uc._is_newer("0.2.1", "0.2.2")


def test_update_available(isolated: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(uc, "_fetch_pypi", lambda: {"info": {"version": "99.0.0"}, "releases": {}})
    info = uc.check_for_update(force=True)
    assert info is not None
    assert info.update_available and info.latest == "99.0.0" and not info.current_yanked
    assert "99.0.0" in info.how_to_update()
    assert "WALLETD_DATADIR" in info.how_to_update()  # the "keys untouched" promise


def test_no_update_when_latest_equals_current(
    isolated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(uc, "_fetch_pypi", lambda: {"info": {"version": uc.__version__}, "releases": {}})
    info = uc.check_for_update(force=True)
    assert info is not None and not info.update_available


def test_current_version_yanked_is_flagged(
    isolated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cur = uc.__version__
    monkeypatch.setattr(
        uc, "_fetch_pypi", lambda: {"info": {"version": cur}, "releases": {cur: [{"yanked": True}]}}
    )
    info = uc.check_for_update(force=True)
    assert info is not None and info.current_yanked
    notice = uc.startup_notice()  # reads the cache the check just wrote
    assert notice is not None and "YANKED" in notice


def test_disabled_returns_none(isolated: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXFER_MCP_NO_UPDATE_CHECK", "1")
    # Even if the network would answer, disabled short-circuits to None.
    monkeypatch.setattr(uc, "_fetch_pypi", lambda: {"info": {"version": "99.0.0"}})
    assert uc.check_for_update(force=True) is None


def test_network_miss_without_cache_returns_none(
    isolated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(uc, "_fetch_pypi", lambda: None)
    assert uc.check_for_update(force=True) is None  # fail-silent, no exception
