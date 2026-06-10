"""``exfer_check_update`` — is a newer exfer-mcp release available? (read-only).

Does not touch walletd or the wallet — it only queries PyPI — so the server
dispatches it WITHOUT the managed-walletd readiness gate (you can check for
updates even if walletd is down).
"""

from __future__ import annotations

from typing import Any

import mcp.types as mcp_types
from exfer import AsyncClient

from ..config import Config
from ..update_check import check_for_update
from ._common import json_text

CHECK_UPDATE_TOOL = mcp_types.Tool(
    name="exfer_check_update",
    description=(
        "Check whether a newer exfer-mcp release is available on PyPI. Read-only: "
        "no wallet access, no spend, no walletd needed. Returns current/latest "
        "version, whether your running version was YANKED (a security recall — "
        "update promptly), and how to update. exfer-mcp NEVER auto-updates; "
        "updating is a deliberate version re-pin and never touches your wallet "
        "keystore/datadir. Call this when the user asks about updates, or proactively "
        "mention it if 'update_available' or 'current_version_yanked' is true."
    ),
    inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
)


async def check_update(
    client: AsyncClient,
    arguments: dict[str, Any],
    config: Config,
) -> list[mcp_types.TextContent]:
    del client, arguments, config  # no wallet access
    info = check_for_update(force=True)
    if info is None:
        return [
            json_text(
                {
                    "update_check": "unavailable",
                    "reason": "disabled via EXFER_MCP_NO_UPDATE_CHECK, or PyPI unreachable",
                }
            )
        ]
    return [
        json_text(
            {
                "current": info.current,
                "latest": info.latest,
                "update_available": info.update_available,
                "current_version_yanked": info.current_yanked,
                "how_to_update": info.how_to_update(),
                "wallet_data_safety": (
                    "Updating swaps exfer-mcp + the SHA-256-verified walletd binary only. "
                    "Your wallet data in WALLETD_DATADIR (keystore, seed, RECOVERY_PHRASE.txt, "
                    "tokens) is never read, moved, or deleted by an update."
                ),
            }
        )
    ]
