"""Shared fixtures for exfer-mcp tests.

We never spawn a real walletd here — every test mocks walletd at the
httpx layer via respx, then drives the MCP tool handlers directly.
That keeps the tests fast and removes the dependency on a tagged
walletd build.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
import respx
from exfer import AsyncClient

from exfer_mcp.config import Config

WALLETD_URL = "http://walletd.test"
TOKEN = "test-token"


@pytest.fixture
def mock_walletd() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=WALLETD_URL, assert_all_called=False) as router:
        yield router


@pytest.fixture
async def client(mock_walletd: respx.MockRouter) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(WALLETD_URL, TOKEN) as c:
        yield c


@pytest.fixture
def config() -> Config:
    """A minimal Config that maps onto the mock walletd fixtures."""
    return Config(
        walletd_url=WALLETD_URL,
        walletd_token=TOKEN,
        walletd_fingerprint=None,
        default_fee_rate=None,
        httpx_timeout=30.0,
    )


def rpc_ok(result: object, *, request_id: int = 1) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "result": result, "id": request_id})


def rpc_err(code: int, message: str, *, request_id: int = 1) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "error": {"code": code, "message": message},
            "id": request_id,
        },
    )
