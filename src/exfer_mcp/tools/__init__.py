"""Tool registry.

Each tool lives in its own module so adding / removing a tool stays
local to one file. The registry collects them here for ``server.py``
to wire into the MCP dispatch.
"""

from __future__ import annotations

import mcp.types as mcp_types

from ._common import ToolHandler
from .address import (
    GENERATE_ADDRESS_TOOL,
    GET_BALANCE_TOOL,
    generate_address,
    get_balance,
)
from .payment_uri import (
    PAYMENT_URI_DECODE_TOOL,
    PAYMENT_URI_ENCODE_TOOL,
    payment_uri_decode,
    payment_uri_encode,
)
from .transfer import (
    SIMULATE_TRANSFER_TOOL,
    TRANSFER_TOOL,
    simulate_transfer,
    transfer,
)
from .wait import WAIT_FOR_TX_TOOL, wait_for_tx

__all__ = ["HANDLERS", "TOOLS"]

# Order matters for the `list_tools` response: the agent reads top-down
# when picking which tool to call, so we put preflight + read tools
# above spend tools — encouraging "simulate, then transfer".
TOOLS: list[mcp_types.Tool] = [
    GENERATE_ADDRESS_TOOL,
    GET_BALANCE_TOOL,
    SIMULATE_TRANSFER_TOOL,
    TRANSFER_TOOL,
    WAIT_FOR_TX_TOOL,
    PAYMENT_URI_ENCODE_TOOL,
    PAYMENT_URI_DECODE_TOOL,
]

HANDLERS: dict[str, ToolHandler] = {
    GENERATE_ADDRESS_TOOL.name: generate_address,
    GET_BALANCE_TOOL.name: get_balance,
    SIMULATE_TRANSFER_TOOL.name: simulate_transfer,
    TRANSFER_TOOL.name: transfer,
    WAIT_FOR_TX_TOOL.name: wait_for_tx,
    PAYMENT_URI_ENCODE_TOOL.name: payment_uri_encode,
    PAYMENT_URI_DECODE_TOOL.name: payment_uri_decode,
}
