# exfer-mcp

Model Context Protocol server for the [Exfer](https://github.com/ahuman-exfer/exfer) blockchain. Gives an AI agent (Claude Desktop, Claude Code, any MCP-aware host) typed, direct access to an [`exfer-walletd`](https://github.com/exfer-stack/exfer-walletd) hot wallet.

> ⚠️ **This is a hot wallet.** Anything that can talk to this MCP server can spend funds — there are no per-period caps, no human-approval gates, no rate limits beyond walletd's own. Until walletd ships the v1.10 allowance ledger, run `exfer-mcp` only against accounts you would be okay losing in full.

## What it exposes

22 tools spanning the full wallet, payment, HTLC, reputation, and EXFER-QUOTE surface.

**Wallet & chain (read)**

| Tool | What it does |
|---|---|
| `exfer_generate_address` | Create a new managed address — returns `{address, pubkey, index}` (use the `pubkey` as a quote's `payee_pubkey`) |
| `exfer_list_addresses` | List the managed addresses (with index / label) |
| `exfer_get_balance` | Confirmed balance of a managed address |
| `exfer_get_block_height` | Current chain tip `{height, block_id}` |

**Payments**

| Tool | What it does |
|---|---|
| `exfer_simulate_transfer` | Dry-run a payment — exact fee + inputs, no broadcast |
| `exfer_transfer` | Build, sign, broadcast a payment (optional on-chain `datum`) |
| `exfer_wait_for_tx` | Block until a tx reaches a confirmation depth |
| `exfer_wait_for_payment` | Block until a payment to an address is seen — sub-second push, no polling |
| `exfer_payment_uri_encode` / `_decode` | Build / parse a BIP21-style `exfer:` URI |

**Identity & EXFER-QUOTE**

| Tool | What it does |
|---|---|
| `exfer_sign_message` / `exfer_verify_message` | Sign / verify an arbitrary message (proof of key control) |
| `exfer_quote_issue` | Issue a signed EXFER-QUOTE price credential (Spend-posture; signs, moves no money) |
| `exfer_quote_verify` | Verify a signed EXFER-QUOTE (pure read) |

**Conditional payment & reputation**

| Tool | What it does |
|---|---|
| `exfer_htlc_lock` / `_claim` / `_reclaim` / `_status` / `_list` | Hash-time-locked contracts (atomic swaps) |
| `exfer_get_address_history` | An address's on-chain activity (indexer-backed) |
| `exfer_get_attestation_edges` / `exfer_detect_in_chain_swaps` | Counterparty reputation / swap detection (indexer-backed) |

> The reputation / history tools and non-owned HTLC lookups require walletd to be pointed at an `exfer-indexer` (it is, by default, in managed mode).

## Two ways to run

`exfer-mcp` talks to an `exfer-walletd` hot wallet. You can either point it at a walletd **you** run, or let it spawn and supervise **its own** — like the browser MCP manages its own headless browser. The mode is selected automatically by whether `WALLETD_URL` is set:

| | **External** | **Managed (zero-setup)** |
|---|---|---|
| Selected when | `WALLETD_URL` **set** | `WALLETD_URL` **unset** |
| Who runs walletd | you, separately | exfer-mcp spawns + supervises it |
| Required env | `WALLETD_URL` + `WALLETD_AUTH_TOKEN` | `WALLETD_KEYSTORE_PASSPHRASE` (+ `EXFER_WALLETD_BIN` if not on `PATH`) |
| Node / indexer | whatever your walletd uses | the project's public mainnet node + indexer (overridable) |
| Lifecycle | independent of the MCP | tied to the MCP — spawned on start, killed on exit (no orphans) |

### 1. External walletd (original behaviour)

You already run `exfer-walletd` somewhere; exfer-mcp just connects to it. Set `WALLETD_URL` + `WALLETD_AUTH_TOKEN` and (for `https://` with a self-signed cert) `WALLETD_FINGERPRINT`. Nothing about this path changed — see [Configure (Claude Desktop)](#configure-claude-desktop) below.

```jsonc
// codex / Claude config — EXTERNAL mode
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

### 2. Managed (zero-setup)

Leave `WALLETD_URL` **unset**. exfer-mcp then spawns its own walletd against the project's **public mainnet reference node + indexer** and wires the rest of the server to it automatically. You only have to provide a keystore passphrase (and a path to the binary if `exfer-walletd` isn't on `PATH`):

```jsonc
// codex / Claude config — MANAGED mode (zero-setup)
{
  "mcpServers": {
    "exfer": {
      "command": "uvx",
      "args": ["exfer-mcp"],
      "env": {
        // No WALLETD_URL → managed mode.
        "WALLETD_KEYSTORE_PASSPHRASE": "<a strong passphrase for the managed wallet>",
        // Only needed if exfer-walletd is not on PATH:
        "EXFER_WALLETD_BIN": "/path/to/exfer-walletd"
      }
    }
  }
}
```

On first run, the managed wallet has no keystore, so exfer-mcp initialises a fresh **seeded** one and prints the 24-word recovery phrase **prominently to stderr** (the MCP host surfaces stderr to you). That phrase is the **only** backup for the funds in this wallet — write it down.

> ⚠️ **The managed wallet is a HOT WALLET.** Anything that can reach this MCP server can spend its funds (see [Safety](#safety)). The keystore + scoped bearer tokens live under `WALLETD_DATADIR` (default `~/.exfer-walletd-mcp`) and persist across restarts, so the phrase is shown **once** — back it up the first time.

Managed mode is **lazy / non-blocking** — the MCP handshake is instant, so the host (codex / Claude) never appears frozen on startup:

1. Picks a loopback bind — `127.0.0.1:7448` by default, or a free loopback port if 7448 is busy (so a managed walletd never collides with a walletd you run yourself) — **synchronously**, so the bind is known immediately.
2. **Answers the MCP handshake + `list_tools` right away** (the tool list is static and needs no walletd); walletd is brought up in the **background**.
3. In the background: initialises a seeded keystore under `WALLETD_DATADIR` if one isn't there yet (surfacing the recovery phrase; reuses an existing keystore otherwise), then spawns `exfer-walletd --datadir … --bind … --node-rpc … --indexer-rpc …` with your passphrase in its env, forwarding its logs to the MCP's stderr with a `[walletd]` prefix (bearer tokens **redacted**).
4. The **first tool call** waits until walletd answers a `get_block_height` health probe (typically a few seconds; up to ~30 s), reads the spend-scope bearer token, and caches the client — so the agent sees at most one slightly-slow first call, never a frozen handshake. A ready-timeout returns a clear error, not a hang.
5. On shutdown (clean exit, `SIGINT`, or `SIGTERM`) terminates walletd — `SIGTERM`, then `SIGKILL` after a grace period; the first-run init child is torn down too. **No orphaned walletd processes.**

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

**`WALLETD_URL` is the mode switch.** Set it → **External** mode (connect to your walletd). Leave it unset → **Managed** mode (exfer-mcp spawns its own walletd against the public mainnet defaults below).

### External mode

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `WALLETD_URL` | ✓ (set → external) | — | walletd base URL |
| `WALLETD_AUTH_TOKEN` | ✓ | — | walletd bearer token |
| `WALLETD_FINGERPRINT` | only for `https://` with a self-signed cert | — | SHA-256 of walletd's TLS cert (`sha256:<hex>`) |

### Managed mode (`WALLETD_URL` unset)

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `WALLETD_KEYSTORE_PASSPHRASE` | ✓ | — | Passphrase to unlock (and, on first run, create) the managed wallet keystore. **Required in managed mode.** |
| `EXFER_WALLETD_BIN` | only if `exfer-walletd` isn't on `PATH` | auto-detect `exfer-walletd` on `PATH` | Full path to the walletd binary |
| `EXFER_NODE_RPC` | | `http://64.176.231.198:9334,http://89.127.232.155:9334` | Upstream Exfer node(s) — the project's public mainnet **reference node + a backup**, comma-separated. walletd round-robins and fails over across them. |
| `EXFER_INDEXER_RPC` | | `http://64.176.231.198:9335` | The project's public mainnet **indexer**, for observability queries that need data outside this wallet's own keys. Set to an **empty string** to disable indexer delegation. |
| `WALLETD_DATADIR` | | `~/.exfer-walletd-mcp` | Where the managed keystore + bearer tokens live. Persists across restarts; reused if present. |
| `EXFER_WALLETD_BIND` | | `127.0.0.1:7448` | Preferred loopback `host:port`. If the port is busy, exfer-mcp picks a free loopback port instead so it never collides with a walletd you run yourself. |

### Common to both modes

| Variable | Required | Default | Meaning |
|---|---|---|---|
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
- **Managed mode is also a hot wallet.** The walletd exfer-mcp spawns binds **loopback only** and its bearer tokens never leave the local machine — exfer-mcp also **redacts** any 64-hex token from walletd's forwarded `[walletd]` log lines, so the first-run spend token never lands in the host's stderr log file. Anything that can reach the MCP server can still spend its funds. The 24-word recovery phrase is printed to stderr **once**, on first run (`BACK UP THIS RECOVERY PHRASE`) — it is the **only** backup for that wallet, so write it down then. Treat `WALLETD_KEYSTORE_PASSPHRASE` and the contents of `WALLETD_DATADIR` (default `~/.exfer-walletd-mcp`) as wallet secrets, and keep only a float you'd be comfortable losing in the managed wallet.

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
