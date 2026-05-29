# exfer-mcp

Model Context Protocol server for the [Exfer](https://github.com/ahuman-exfer/exfer) blockchain. Gives an AI agent (Claude Desktop, Claude Code, any MCP-aware host) typed, direct access to an [`exfer-walletd`](https://github.com/exfer-stack/exfer-walletd) hot wallet.

> ⚠️ **This is a hot wallet.** Anything that can talk to this MCP server can spend funds — there are no per-period caps, no human-approval gates, no rate limits beyond walletd's own. Until walletd ships the v1.10 allowance ledger, run `exfer-mcp` only against accounts you would be okay losing in full.

## What it exposes

Seven v0.1 tools — enough for the "Hello World agent flow" of generating an address, simulating a transfer, sending it, and waiting for confirmation:

| Tool | What it does |
|---|---|
| `exfer_generate_address` | Create a new managed wallet address |
| `exfer_get_balance` | Confirmed balance of a managed address |
| `exfer_simulate_transfer` | Dry-run a payment — exact fee + inputs, no broadcast |
| `exfer_transfer` | Build, sign, broadcast a payment |
| `exfer_wait_for_tx` | Block until a tx reaches a confirmation depth |
| `exfer_payment_uri_encode` | Build a BIP21-style `exfer:` URI |
| `exfer_payment_uri_decode` | Parse a BIP21-style `exfer:` URI |

HTLC swap tools, attestation / reputation lookups, and `htlc_list` are out of v0.1 scope — tracked for v0.2.

## Install

```bash
pip install exfer-mcp
```

Requires Python ≥ 3.10. Pulls `exfer-walletd ≥ 0.8.0` (the JSON-RPC client) and `mcp ≥ 1.0` (the MCP server framework).

## Configure (Claude Desktop)

Add the following to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or the equivalent on your OS, then restart Claude Desktop:

```json
{
  "mcpServers": {
    "exfer": {
      "command": "exfer-mcp",
      "env": {
        "WALLETD_URL": "http://127.0.0.1:7448",
        "WALLETD_AUTH_TOKEN": "<paste your walletd token here>"
      }
    }
  }
}
```

The token is whatever `exfer-walletd` was started with — by default it's written to `~/.exfer-walletd/token` on first run (`chmod 0600`).

If walletd is running with `--tls`, use an `https://` URL and pin its cert:

```json
{
  "mcpServers": {
    "exfer": {
      "command": "exfer-mcp",
      "env": {
        "WALLETD_URL": "https://127.0.0.1:7448",
        "WALLETD_AUTH_TOKEN": "<token>",
        "WALLETD_FINGERPRINT": "sha256:<paste from cert.fingerprint>"
      }
    }
  }
}
```

## Configure (Claude Code)

`claude mcp add` or edit your project / global config:

```json
{
  "mcpServers": {
    "exfer": {
      "command": "exfer-mcp",
      "env": {
        "WALLETD_URL": "http://127.0.0.1:7448",
        "WALLETD_AUTH_TOKEN": "<token>"
      }
    }
  }
}
```

## Environment

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `WALLETD_URL` | ✓ | — | walletd base URL |
| `WALLETD_AUTH_TOKEN` | ✓ | — | walletd bearer token |
| `WALLETD_FINGERPRINT` | only for `https://` | — | SHA-256 of walletd's TLS cert |
| `EXFER_MCP_DEFAULT_FEE_RATE` | | walletd default | fee_rate (exfers/byte) for spends when the agent didn't specify |
| `EXFER_MCP_HTTPX_TIMEOUT` | | 30 | per-RPC timeout in seconds |

## Recommended agent flow

When the user asks the agent to send a payment, the expected sequence is:

1. `exfer_simulate_transfer` → confirm exact fee
2. Show the user the fee and ask for confirmation
3. `exfer_transfer` → broadcast
4. `exfer_wait_for_tx` → confirm

The simulate-first pattern means the agent always knows the cost before committing. The user is the one who decides whether the cost is acceptable.

## Safety

- Read `WALLETD_AUTH_TOKEN` is **all-or-nothing access to the wallet**. Treat it like a payment-card number.
- `exfer-mcp` does no per-call confirmation by itself — that's the host's job. If you need spend caps, configure them on the walletd side (planned for v1.10) or run a walletd that only holds a small float you would be comfortable losing.
- The MCP transport is stdio. The agent does not see the wire token; only this process does.
- Errors from walletd surface as MCP `isError=true` content the agent reads and reacts to, including specific cases like `InsufficientBalanceError` (over-spend) and `WaitTimeoutError` (confirmation depth not reached in time).

## Development

```bash
pip install -e '.[dev]'
pytest
mypy
ruff check
```

## License

MIT
