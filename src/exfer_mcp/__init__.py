"""Model Context Protocol server for the Exfer blockchain.

Wraps an ``exfer-walletd`` daemon as a set of MCP tools so an AI agent
(Claude Desktop, Claude Code, any MCP-aware host) can transact on the
Exfer chain directly — generate addresses, simulate transfers, send
payments, wait for confirmation, build payment URIs.

The transport is stdio; the agent host spawns this process as a
subprocess and speaks JSON-RPC over its stdin / stdout. See the README
for the exact host-config stanza.

.. warning::

   This is a hot wallet. Anything that can talk to this MCP server can
   spend funds from the wired ``WALLETD_AUTH_TOKEN``. Run only against
   accounts you would be okay losing in full until walletd ships
   per-period allowance caps (tracked at exfer-walletd v1.10).
"""

from __future__ import annotations

from ._version import __version__

__all__ = ["__version__"]
