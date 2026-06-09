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
    GET_BLOCK_HEIGHT_TOOL,
    LIST_ADDRESSES_TOOL,
    generate_address,
    get_balance,
    get_block_height,
    list_addresses,
)
from .htlc import (
    HTLC_CLAIM_TOOL,
    HTLC_LIST_TOOL,
    HTLC_LOCK_TOOL,
    HTLC_RECLAIM_TOOL,
    HTLC_STATUS_TOOL,
    htlc_claim,
    htlc_list,
    htlc_lock,
    htlc_reclaim,
    htlc_status,
)
from .payment_uri import (
    PAYMENT_URI_DECODE_TOOL,
    PAYMENT_URI_ENCODE_TOOL,
    payment_uri_decode,
    payment_uri_encode,
)
from .quote import (
    QUOTE_ISSUE_TOOL,
    QUOTE_VERIFY_TOOL,
    quote_issue,
    quote_verify,
)
from .reputation import (
    DETECT_IN_CHAIN_SWAPS_TOOL,
    GET_ADDRESS_HISTORY_TOOL,
    GET_ATTESTATION_EDGES_TOOL,
    detect_in_chain_swaps,
    get_address_history,
    get_attestation_edges,
)
from .signing import (
    SIGN_MESSAGE_TOOL,
    VERIFY_MESSAGE_TOOL,
    sign_message,
    verify_message,
)
from .transfer import (
    SIMULATE_TRANSFER_TOOL,
    TRANSFER_TOOL,
    simulate_transfer,
    transfer,
)
from .wait import (
    WAIT_FOR_PAYMENT_TOOL,
    WAIT_FOR_TX_TOOL,
    wait_for_payment,
    wait_for_tx,
)

__all__ = ["HANDLERS", "TOOLS"]

# Order matters for the `list_tools` response: the agent reads top-down
# when picking which tool to call, so we put preflight + read tools
# above spend tools — encouraging "simulate, then transfer".
TOOLS: list[mcp_types.Tool] = [
    GENERATE_ADDRESS_TOOL,
    LIST_ADDRESSES_TOOL,
    GET_BALANCE_TOOL,
    GET_BLOCK_HEIGHT_TOOL,
    SIMULATE_TRANSFER_TOOL,
    TRANSFER_TOOL,
    # signed price credential (EXFER-QUOTE)
    QUOTE_ISSUE_TOOL,
    WAIT_FOR_TX_TOOL,
    WAIT_FOR_PAYMENT_TOOL,
    PAYMENT_URI_ENCODE_TOOL,
    PAYMENT_URI_DECODE_TOOL,
    QUOTE_VERIFY_TOOL,
    # identity / proof-of-control
    SIGN_MESSAGE_TOOL,
    VERIFY_MESSAGE_TOOL,
    # conditional payment (HTLC)
    HTLC_LOCK_TOOL,
    HTLC_CLAIM_TOOL,
    HTLC_RECLAIM_TOOL,
    HTLC_STATUS_TOOL,
    HTLC_LIST_TOOL,
    # counterparty reputation / history
    GET_ADDRESS_HISTORY_TOOL,
    GET_ATTESTATION_EDGES_TOOL,
    DETECT_IN_CHAIN_SWAPS_TOOL,
]

HANDLERS: dict[str, ToolHandler] = {
    GENERATE_ADDRESS_TOOL.name: generate_address,
    LIST_ADDRESSES_TOOL.name: list_addresses,
    GET_BALANCE_TOOL.name: get_balance,
    GET_BLOCK_HEIGHT_TOOL.name: get_block_height,
    SIMULATE_TRANSFER_TOOL.name: simulate_transfer,
    TRANSFER_TOOL.name: transfer,
    QUOTE_ISSUE_TOOL.name: quote_issue,
    QUOTE_VERIFY_TOOL.name: quote_verify,
    WAIT_FOR_TX_TOOL.name: wait_for_tx,
    WAIT_FOR_PAYMENT_TOOL.name: wait_for_payment,
    PAYMENT_URI_ENCODE_TOOL.name: payment_uri_encode,
    PAYMENT_URI_DECODE_TOOL.name: payment_uri_decode,
    SIGN_MESSAGE_TOOL.name: sign_message,
    VERIFY_MESSAGE_TOOL.name: verify_message,
    HTLC_LOCK_TOOL.name: htlc_lock,
    HTLC_CLAIM_TOOL.name: htlc_claim,
    HTLC_RECLAIM_TOOL.name: htlc_reclaim,
    HTLC_STATUS_TOOL.name: htlc_status,
    HTLC_LIST_TOOL.name: htlc_list,
    GET_ADDRESS_HISTORY_TOOL.name: get_address_history,
    GET_ATTESTATION_EDGES_TOOL.name: get_attestation_edges,
    DETECT_IN_CHAIN_SWAPS_TOOL.name: detect_in_chain_swaps,
}
