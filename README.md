# exfer-mcp

Model Context Protocol server for the [Exfer](https://github.com/ahuman-exfer/exfer) blockchain. Gives an AI agent (Claude Desktop, Claude Code, any MCP-aware host) typed, direct access to an [`exfer-walletd`](https://github.com/exfer-stack/exfer-walletd) hot wallet.

> ⚠️ **This is a hot wallet.** Anything that can talk to this MCP server can spend funds — there are no per-period caps, no human-approval gates, no rate limits beyond walletd's own. Until walletd ships the v1.10 allowance ledger, run `exfer-mcp` only against accounts you would be okay losing in full.

## What it exposes

Eight tools — enough for the "Hello World agent flow" of generating an address, simulating a transfer, sending it, waiting for confirmation, and being notified the instant a payment arrives:

| Tool | What it does |
|---|---|
| `exfer_generate_address` | Create a new managed wallet address |
| `exfer_get_balance` | Confirmed balance of a managed address |
| `exfer_simulate_transfer` | Dry-run a payment — exact fee + inputs, no broadcast |
| `exfer_transfer` | Build, sign, broadcast a payment |
| `exfer_wait_for_tx` | Block until a tx reaches a confirmation depth |
| `exfer_wait_for_payment` | Block until a payment to an address is seen — sub-second push, no polling |
| `exfer_payment_uri_encode` | Build a BIP21-style `exfer:` URI |
| `exfer_payment_uri_decode` | Parse a BIP21-style `exfer:` URI |

HTLC swap tools, attestation / reputation lookups, and `htlc_list` are out of v0.1 scope — tracked for v0.2.

## Install

### Recommended — `uvx` (zero global install)

`uvx` runs the server in an isolated, on-demand environment. No `pip install`, no virtual-env management, no PATH wrestling. The host (Claude Desktop / Claude Code / Cursor / …) spawns it on demand and uv handles the rest.

Install `uv` once:

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Then wire `uvx exfer-mcp` into your MCP host config — examples below. The first time the host launches the server, uv fetches the package from PyPI; subsequent runs are cached.

### Fallback — `pip install`

For developer / CI use, or environments where adding `uv` is awkward:

```bash
pip install exfer-mcp        # installs the `exfer-mcp` console script
```

Requires Python ≥ 3.10. Pulls `exfer-walletd ≥ 0.8.0` (the JSON-RPC client) and `mcp ≥ 1.0` (the MCP server framework).

## Configure (Claude Desktop)

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS (or the equivalent path on your OS), then restart Claude Desktop:

```json
{
  "mcpServers": {
    "exfer": {
      "command": "uvx",
      "args": ["exfer-mcp"],
      "env": {
        "WALLETD_URL": "http://127.0.0.1:7448",
        "WALLETD_AUTH_TOKEN": "<paste your walletd token here>"
      }
    }
  }
}
```

The token is whatever `exfer-walletd` was started with — by default it's written to `~/.exfer-walletd/token` on first run (`chmod 0600`).

If walletd is running with `--tls` and a self-signed cert, pin its fingerprint:

```json
{
  "mcpServers": {
    "exfer": {
      "command": "uvx",
      "args": ["exfer-mcp"],
      "env": {
        "WALLETD_URL": "https://127.0.0.1:7448",
        "WALLETD_AUTH_TOKEN": "<token>",
        "WALLETD_FINGERPRINT": "sha256:<paste from cert.fingerprint>"
      }
    }
  }
}
```

For a publicly-fronted walletd (e.g. behind a reverse proxy with a CA-signed cert), drop the `WALLETD_FINGERPRINT` field — the SDK falls back to the system CA chain.

### Pin a specific version

```json
"args": ["exfer-mcp@0.1.0"]
```

## Configure (Claude Code)

One-shot via `claude mcp add`:

```bash
claude mcp add exfer \
  -e WALLETD_URL=http://127.0.0.1:7448 \
  -e WALLETD_AUTH_TOKEN=<token> \
  -- uvx exfer-mcp
```

Or by editing the project / global Claude Code MCP config directly:

```json
{
  "mcpServers": {
    "exfer": {
      "command": "uvx",
      "args": ["exfer-mcp"],
      "env": {
        "WALLETD_URL": "http://127.0.0.1:7448",
        "WALLETD_AUTH_TOKEN": "<token>"
      }
    }
  }
}
```

## Configure (other MCP hosts)

Cursor, Cline, Continue.dev, and most other MCP-aware hosts accept the same `command` / `args` / `env` shape. Use the `uvx exfer-mcp` invocation above.

## Environment

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `WALLETD_URL` | ✓ | — | walletd base URL |
| `WALLETD_AUTH_TOKEN` | ✓ | — | walletd bearer token |
| `WALLETD_FINGERPRINT` | only for `https://` with a self-signed cert | — | SHA-256 of walletd's TLS cert (`sha256:<hex>`) |
| `EXFER_MCP_DEFAULT_FEE_RATE` | | walletd default | fee_rate (exfers/byte) for spends when the agent didn't specify |
| `EXFER_MCP_HTTPX_TIMEOUT` | | 30 | per-RPC timeout in seconds |

## Recommended agent flow

When the user asks the agent to send a payment, the expected sequence is:

1. `exfer_simulate_transfer` → compute exact fee
2. Show the user the fee and ask for confirmation
3. `exfer_transfer` → broadcast
4. `exfer_wait_for_tx` → confirm

The simulate-first pattern means the agent always knows the cost before committing. The user is the one who decides whether the cost is acceptable.

## Safety

- `WALLETD_AUTH_TOKEN` is **all-or-nothing access to the wallet**. Treat it like a payment-card number.
- `exfer-mcp` does no per-call confirmation by itself — that's the host's job. If you need spend caps, configure them on the walletd side (planned for v1.10) or run a walletd that only holds a small float you would be comfortable losing.
- The MCP transport is stdio. The agent does not see the wire token; only this process does.
- Errors from walletd surface as MCP `isError=true` content the agent reads and reacts to, including specific cases like `InsufficientBalanceError` (over-spend) and `WaitTimeoutError` (confirmation depth not reached in time).

## Coming soon

- **`.mcpb` desktop bundle** — Anthropic's one-click `.mcpb` install format for Claude Desktop, with the env vars surfaced as a form at install time.
- **v0.2 tools** — HTLC trio (`exfer_htlc_lock` / `exfer_htlc_claim` / `exfer_htlc_reclaim`), `exfer_htlc_status`, attestation queries (`exfer_get_attestation_edges`).
- **MCP directory** — submission to Anthropic's curated MCP directory + community directories.

## Development

```bash
git clone https://github.com/exfer-stack/exfer-mcp
cd exfer-mcp
uv venv && source .venv/bin/activate
uv pip install -e '.[dev]'
pytest                          # unit tests
mypy && ruff check               # lint
python -m scripts.e2e_smoke      # end-to-end smoke (needs a live walletd)
```

See [`scripts/e2e_smoke.py`](scripts/e2e_smoke.py) for a runnable example that exercises the full Exfer stack — walletd, indexer, MCP — against a real deployment.

## License

MIT
