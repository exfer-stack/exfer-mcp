# exfer-mcp

MCP server for the [Exfer](https://github.com/ahuman-exfer/exfer) blockchain — gives an AI agent (Claude Code, Claude Desktop, Cursor, any MCP host) a typed wallet it can actually use: balances, payments, signed price quotes, and HTLC settlement on mainnet.

> ⚠️ **It's a hot wallet.** Anything that can reach this server can spend it. Only fund it with what you're OK losing, or set spend caps (`WALLETD_SPEND_CAP_PER_TX`, `WALLETD_SPEND_CAP_PER_PERIOD` + `WALLETD_SPEND_CAP_PERIOD_SECS`, in exfers).

## One-paste setup (hand this to your agent)

Paste this into a coding agent that can run shell commands (Claude Code, Cursor, …):

```text
Set up the "exfer" MCP server for me in managed (zero-setup) mode:

1. If `uv` isn't installed, install it:
   - macOS/Linux: curl -LsSf https://astral.sh/uv/install.sh | sh
   - Windows: powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
2. Generate a strong random passphrase for the managed hot wallet; show it to me and keep it.
3. Register the server (Claude Code):
   claude mcp add exfer -e WALLETD_KEYSTORE_PASSPHRASE='<that passphrase>' -- uvx exfer-mcp
   (Other hosts: add an MCP server with command `uvx`, args ["exfer-mcp"], and that env var.)
4. Reconnect the server, then call exfer_get_block_height to confirm it returns a height.
5. If it errors that the walletd binary can't be downloaded/verified, grab the binary for my
   platform from https://github.com/exfer-stack/exfer-walletd/releases/latest , chmod +x it,
   and re-register with an extra `-e EXFER_WALLETD_BIN=/full/path/to/exfer-walletd`; retry step 4.
6. On first run it prints a 24-word recovery phrase to its logs — capture it and give it to me
   (it's the ONLY backup for this wallet), then tell me a receive address via exfer_generate_address.

Reminder: this is a hot wallet — I'll only fund it with an amount I'm OK losing.
```

## Install (manual)

`exfer-mcp` is on PyPI. The recommended launcher is **`uvx`** — no global install, the host runs it on demand:

```bash
# one-time: install uv (macOS/Linux)
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then point your MCP host at `uvx exfer-mcp` (configs below). Or `pip install exfer-mcp` (Python ≥ 3.10; pulls the `exfer` SDK, `mcp`, and `psutil`).

## Configure

`exfer-mcp` needs an `exfer-walletd` hot wallet. **Setting `WALLETD_URL` is the mode switch:**

### Managed (zero-setup) — recommended

Leave `WALLETD_URL` unset. exfer-mcp spawns + supervises its own walletd against Exfer's public mainnet node + indexer, and **obtains the walletd binary automatically**: `EXFER_WALLETD_BIN` → `exfer-walletd` on `PATH` → else it downloads the prebuilt binary for your platform and verifies it against the release's `SHA256SUMS` before running it (cached in `~/.cache/exfer-mcp/walletd/`). You only provide a passphrase:

```jsonc
{
  "mcpServers": {
    "exfer": {
      "command": "uvx",
      "args": ["exfer-mcp"],
      "env": { "WALLETD_KEYSTORE_PASSPHRASE": "<a strong passphrase>" }
    }
  }
}
```

First run creates a seeded keystore and prints its 24-word recovery phrase to stderr **once** — that's the only backup. Keystore + datadir live in `WALLETD_DATADIR` (default `~/.exfer-walletd-mcp`) and persist across restarts.

Claude Code one-liner:

```bash
claude mcp add exfer -e WALLETD_KEYSTORE_PASSPHRASE='<passphrase>' -- uvx exfer-mcp
```

### External — connect to a walletd you run

Set `WALLETD_URL` + `WALLETD_AUTH_TOKEN` (and `WALLETD_FINGERPRINT` for `https://` with a self-signed cert):

```jsonc
{
  "mcpServers": {
    "exfer": {
      "command": "uvx",
      "args": ["exfer-mcp"],
      "env": {
        "WALLETD_URL": "http://127.0.0.1:7448",
        "WALLETD_AUTH_TOKEN": "<walletd token>"
      }
    }
  }
}
```

### Environment reference

| Variable | Mode | Default | Meaning |
|---|---|---|---|
| `WALLETD_KEYSTORE_PASSPHRASE` | managed (required) | — | unlocks / creates the managed keystore |
| `EXFER_WALLETD_BIN` | managed (optional) | auto: PATH or download | path to a walletd binary (skips auto-download) |
| `EXFER_WALLETD_VERSION` | managed (optional) | pinned | walletd release to auto-download |
| `WALLETD_DATADIR` | managed (optional) | `~/.exfer-walletd-mcp` | keystore + tokens; **give each concurrent session its own** |
| `EXFER_NODE_RPC` / `EXFER_INDEXER_RPC` | managed (optional) | public mainnet | upstream node(s) / indexer (`""` indexer = disable) |
| `WALLETD_URL` + `WALLETD_AUTH_TOKEN` | external (required) | — | walletd URL + bearer token |
| `WALLETD_FINGERPRINT` | external (optional) | — | `sha256:<hex>` for self-signed TLS |

> Running **multiple** agent sessions at once? Managed mode is one wallet per datadir — give each session a distinct `WALLETD_DATADIR`, or run one shared walletd and connect every session in external mode.

## What you get (22 tools)

- **Wallet & chain:** `generate_address`, `list_addresses`, `get_balance`, `get_block_height`
- **Payments:** `simulate_transfer` (dry-run fee), `transfer`, `wait_for_tx`, `wait_for_payment` (push, no polling), `payment_uri_encode`/`_decode`
- **Identity & price quotes:** `sign_message`/`verify_message`, `quote_issue`/`quote_verify` (signed EXFER-QUOTE credentials)
- **Conditional payment:** `htlc_lock`/`_claim`/`_reclaim`/`_status`/`_list` (atomic, hash-time-locked settlement)
- **History:** `get_address_history` (indexer-backed raw activity)

The intended spend flow is **simulate → confirm with the user → transfer → wait** — the agent always knows the fee before committing, and the human decides.

## Safety

- `WALLETD_AUTH_TOKEN` / `WALLETD_KEYSTORE_PASSPHRASE` and the `WALLETD_DATADIR` contents are wallet secrets — full spend authority. The managed walletd binds loopback-only, and exfer-mcp redacts bearer tokens from forwarded logs.
- No per-call human gate is built in (that's the host's job). Bound the blast radius with walletd spend caps, or keep only a small float.
- Auto-downloaded walletd binaries are run **only** after their SHA-256 matches the release `SHA256SUMS`; a mismatch or a checksum-less release is refused.

## License

MIT
