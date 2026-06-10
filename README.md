# exfer-mcp

MCP server for the [Exfer](https://github.com/ahuman-exfer/exfer) blockchain — gives an AI agent (Claude Code, Claude Desktop, Cursor, any MCP host) a typed wallet it can actually use: balances, payments, signed price quotes, and HTLC settlement on mainnet.

> ⚠️ **The agent can spend this wallet on its own — there is no per-transaction human approval.** Unlike a normal wallet (where you click "confirm" on each send), anything that can reach this MCP server — your agent, or a prompt-injection of it — can move the funds. Keep only a float you're OK losing, and/or set spend caps (`WALLETD_SPEND_CAP_PER_TX`, `WALLETD_SPEND_CAP_PER_PERIOD` + `WALLETD_SPEND_CAP_PERIOD_SECS`, in exfers).

## Install

A plain step-by-step. Do it yourself, or hand the steps to an agent **one at a time** — don't paste a wall of instructions at a wallet tool. Managed mode needs only a passphrase.

**1. Install `uv`** (it provides `uvx`):
- macOS / Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Windows: `irm https://astral.sh/uv/install.ps1 | iex`

**2. Pick a keystore passphrase — *you* choose it, not the agent.** It unlocks (and, on first run, creates) the wallet and is stored in your MCP host config. Want a strong random one? `uv run --no-project python -c "import secrets; print(secrets.token_urlsafe(24))"`.

**3. Warm the cache once.** The first launch downloads exfer-mcp + the `walletd` binary (SHA-256-verified) — that can exceed a host's MCP startup timeout. Run it once so the host connects instantly afterward:
```bash
WALLETD_KEYSTORE_PASSPHRASE='<your passphrase>' uvx exfer-mcp==0.3.3 </dev/null
```
Wait until it logs `[walletd] … ready`, then it exits on EOF. (Windows PowerShell: `$env:WALLETD_KEYSTORE_PASSPHRASE='<your passphrase>'; '' | uvx exfer-mcp==0.3.3`.)

**4. Register the server** (managed mode — passphrase only):
- **Claude Code:** `claude mcp add exfer -e WALLETD_KEYSTORE_PASSPHRASE='<your passphrase>' -- uvx exfer-mcp==0.3.3`
- **Claude Desktop / Cursor / Codex:** add under `mcpServers` in the host config:
  ```jsonc
  "exfer": { "command": "uvx", "args": ["exfer-mcp==0.3.3"], "env": { "WALLETD_KEYSTORE_PASSPHRASE": "<your passphrase>" } }
  ```
  To check it registered, confirm the config simply *has* the env key — don't run `claude mcp get` / `mcp list -v`, which print the passphrase back to your screen.

**5. Reload the MCP host**, then call `exfer_get_block_height` (a height = live) and `exfer_generate_address` (your receive address).

**6. Back up the recovery phrase.** First run writes the 24 words to `~/.exfer-walletd-mcp/RECOVERY_PHRASE.txt` (mode 0600). Copy them offline, then delete that file — it plus the passphrase are the only way to recover the wallet.

(Pin the version — `uvx` otherwise resolves+caches whatever is latest. `pip install exfer-mcp` also works for non-uvx setups; Python ≥ 3.10, pulls the `exfer` SDK + `mcp` + `psutil`.)

## Configure

`exfer-mcp` needs an `exfer-walletd` wallet daemon (it holds the keys and signs/broadcasts). **Setting `WALLETD_URL` is the mode switch:**

### Managed (zero-setup) — recommended

Leave `WALLETD_URL` unset. exfer-mcp spawns + supervises its own walletd against Exfer's public mainnet node + indexer, and **obtains the walletd binary automatically**: `EXFER_WALLETD_BIN` → `exfer-walletd` on `PATH` → else it downloads the prebuilt binary for your platform and verifies it against a SHA-256 **baked into this exfer-mcp release** (not a co-located checksum) before running it, re-checking on every run (cached `0o700` in `~/.cache/exfer-mcp/walletd/`). You only provide a passphrase:

```jsonc
{
  "mcpServers": {
    "exfer": {
      "command": "uvx",
      "args": ["exfer-mcp==0.3.3"],
      "env": { "WALLETD_KEYSTORE_PASSPHRASE": "<a strong passphrase>" }
    }
  }
}
```

First run creates a seeded keystore and prints its 24-word recovery phrase to stderr **once** — that's the only backup. Keystore + datadir live in `WALLETD_DATADIR` (default `~/.exfer-walletd-mcp`) and persist across restarts.

Claude Code one-liner:

```bash
claude mcp add exfer -e WALLETD_KEYSTORE_PASSPHRASE='<passphrase>' -- uvx exfer-mcp==0.3.3
```

### External — connect to a walletd you run

Set `WALLETD_URL` + `WALLETD_AUTH_TOKEN` (and `WALLETD_FINGERPRINT` for `https://` with a self-signed cert):

```jsonc
{
  "mcpServers": {
    "exfer": {
      "command": "uvx",
      "args": ["exfer-mcp==0.3.3"],
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

## What you get (23 tools)

- **Wallet & chain:** `generate_address`, `list_addresses`, `get_balance`, `get_block_height`
- **Payments:** `simulate_transfer` (dry-run fee), `transfer`, `wait_for_tx`, `wait_for_payment` (push, no polling), `payment_uri_encode`/`_decode`
- **Identity & price quotes:** `sign_message`/`verify_message`, `quote_issue`/`quote_verify` (signed EXFER-QUOTE credentials)
- **Conditional payment:** `htlc_lock`/`_claim`/`_reclaim`/`_status`/`_list` (atomic, hash-time-locked settlement)
- **History:** `get_address_history` (indexer-backed raw activity)
- **Meta:** `check_update` (is a newer exfer-mcp on PyPI? read-only, no wallet access)

The intended spend flow is **simulate → confirm with the user → transfer → wait** — the agent always knows the fee before committing, and the human decides.

## Updating

exfer-mcp **never updates itself** — silently pulling new code or a new key-handling binary is a supply-chain risk, so an update is always a deliberate step.

- **Check:** call `exfer_check_update` (or ask your agent "is there an exfer-mcp update?"). It reports the latest version, whether your version was **yanked** (a security recall), and the exact update command — read-only, no wallet access, works even if walletd is down. The server also checks ~once a day on startup and notes a newer/yanked release on stderr. Opt out with `EXFER_MCP_NO_UPDATE_CHECK=1`.
- **Update:** bump the pinned version and reload your host — re-add with `uvx exfer-mcp==<new>` (Claude Code) or change `args` in the config file. (Unpinned `uvx exfer-mcp`: `uvx --refresh exfer-mcp`. uv tool / pipx: `uv tool upgrade` / `pipx upgrade`.)
- **Your wallet survives updates.** An update swaps the Python code + the (re-verified) walletd binary only. Your keystore, seed, `RECOVERY_PHRASE.txt`, and tokens in `WALLETD_DATADIR` are **never read, moved, or deleted** — they live in a separate directory from the disposable binary cache (`~/.cache/exfer-mcp`).

## Safety

- `WALLETD_AUTH_TOKEN` / `WALLETD_KEYSTORE_PASSPHRASE` and the `WALLETD_DATADIR` contents are wallet secrets — full spend authority. The managed walletd binds loopback-only, and exfer-mcp redacts bearer tokens from forwarded logs.
- No per-call human gate is built in (that's the host's job). Bound the blast radius with walletd spend caps, or keep only a small float.
- Auto-downloaded walletd binaries are run **only** after their SHA-256 matches a digest **baked into this exfer-mcp release** — re-verified on every run, not just first download. The trust anchor is the PyPI package's Trusted-Publishing provenance, **not** the mutable GitHub release (a co-located `SHA256SUMS` would be worthless against a release/account compromise). A mismatch or an unpinned walletd version is refused; `EXFER_WALLETD_BIN` overrides with a binary you built/trust.

## License

MIT
