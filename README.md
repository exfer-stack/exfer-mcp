# exfer-mcp

MCP server for the [Exfer](https://github.com/ahuman-exfer/exfer) blockchain — gives an AI agent (Claude Code, Claude Desktop, Cursor, any MCP host) a typed wallet it can actually use: balances, payments, signed price quotes, and HTLC settlement on mainnet.

> ⚠️ **It's a hot wallet.** Anything that can reach this server can spend it. Only fund it with what you're OK losing, or set spend caps (`WALLETD_SPEND_CAP_PER_TX`, `WALLETD_SPEND_CAP_PER_PERIOD` + `WALLETD_SPEND_CAP_PERIOD_SECS`, in exfers).

## Provenance & trust (read before funding)

`exfer-mcp` is the Exfer project's official agent-wallet tooling, published by the **`exfer-stack`** org — which also ships the daemon it drives ([`exfer-walletd`](https://github.com/exfer-stack/exfer-walletd)) and the [`exfer`](https://github.com/exfer-stack/exfer-py) Python SDK. It is a **hot-wallet** package, so verify before you trust it:

- **Source is public + auditable** — this repo plus the daemon and SDK above. Read what you run.
- **PyPI provenance** — every release is published *from this repo* via [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/) (OIDC, no long-lived token); the [PyPI page](https://pypi.org/project/exfer-mcp/) shows a signed attestation tying each artifact back to this repo + workflow.
- **Binary verification** — managed mode runs an `exfer-walletd` binary only if its SHA-256 matches a digest **baked into this (auditable) package** (re-checked every run). The trust anchor is this package, *not* a mutable GitHub release. Prefer your own build? Set `EXFER_WALLETD_BIN`.
- **Pin the version** — use `exfer-mcp==0.2.2` (as below), so you run a specific reviewed release.

If you can't independently confirm this package is the project's (e.g. via a link from the official site), **don't fund it** — treat any wallet tool you can't verify as hostile. See [SECURITY.md](SECURITY.md).

## Set it up — paste this to your agent

Works with any agent that can run shell commands and edit config (Claude Code, Claude Desktop, Cursor, Codex, …). It keeps wallet secrets out of the chat:

> Set up the **exfer** MCP server for me — a hot wallet for the Exfer chain. Treat it as a young project: the `walletd` binary is only run after its SHA-256 matches a digest baked into exfer-mcp, and it binds to loopback, but anything that can reach the server can spend the wallet, so I'll keep only a small float.
>
> 1. **Ensure `uv` is installed** (it provides `uvx`): macOS/Linux `curl -LsSf https://astral.sh/uv/install.sh | sh`; Windows `irm https://astral.sh/uv/install.ps1 | iex`.
> 2. **Make a fresh passphrase** with `uv run --no-project python -c "import secrets; print(secrets.token_urlsafe(24))"` and use it as the value of `WALLETD_KEYSTORE_PASSPHRASE` — inject it directly (e.g. shell command substitution) so the literal **never appears in this chat**.
> 3. **Register an MCP server** named `exfer` in whatever host I use — command `uvx`, args `["exfer-mcp==0.2.2"]`, env `WALLETD_KEYSTORE_PASSPHRASE`. Claude Code: `claude mcp add exfer -e WALLETD_KEYSTORE_PASSPHRASE=… -- uvx exfer-mcp==0.2.2`. Claude Desktop / Cursor / Codex: add that block under `mcpServers` in the host's config file.
> 4. **Reload MCP servers, then call `exfer_get_block_height`** — a height means it's live. (The first call downloads + checksum-verifies the `walletd` binary, so it may take a few seconds.) Then give me a receive address via `exfer_generate_address`.
> 5. First run logs a **24-word recovery phrase once** — the wallet's only backup. **Don't repeat it in this chat;** tell me to copy it from the host's MCP server log and keep it offline.

Prefer to wire it by hand? See [Configure](#configure) below.

## Install (manual)

`exfer-mcp` is on PyPI. The recommended launcher is **`uvx`** — no global install, the host runs it on demand:

```bash
# one-time: install uv (macOS/Linux)
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then point your MCP host at `uvx exfer-mcp==0.2.2` (configs below) — pin the version so installs are reproducible (`uvx` otherwise resolves+caches whatever is latest). Or `pip install exfer-mcp` (Python ≥ 3.10; pulls the `exfer` SDK, `mcp`, and `psutil`).

## Configure

`exfer-mcp` needs an `exfer-walletd` hot wallet. **Setting `WALLETD_URL` is the mode switch:**

### Managed (zero-setup) — recommended

Leave `WALLETD_URL` unset. exfer-mcp spawns + supervises its own walletd against Exfer's public mainnet node + indexer, and **obtains the walletd binary automatically**: `EXFER_WALLETD_BIN` → `exfer-walletd` on `PATH` → else it downloads the prebuilt binary for your platform and verifies it against a SHA-256 **baked into this exfer-mcp release** (not a co-located checksum) before running it, re-checking on every run (cached `0o700` in `~/.cache/exfer-mcp/walletd/`). You only provide a passphrase:

```jsonc
{
  "mcpServers": {
    "exfer": {
      "command": "uvx",
      "args": ["exfer-mcp==0.2.2"],
      "env": { "WALLETD_KEYSTORE_PASSPHRASE": "<a strong passphrase>" }
    }
  }
}
```

First run creates a seeded keystore and prints its 24-word recovery phrase to stderr **once** — that's the only backup. Keystore + datadir live in `WALLETD_DATADIR` (default `~/.exfer-walletd-mcp`) and persist across restarts.

Claude Code one-liner:

```bash
claude mcp add exfer -e WALLETD_KEYSTORE_PASSPHRASE='<passphrase>' -- uvx exfer-mcp==0.2.2
```

### External — connect to a walletd you run

Set `WALLETD_URL` + `WALLETD_AUTH_TOKEN` (and `WALLETD_FINGERPRINT` for `https://` with a self-signed cert):

```jsonc
{
  "mcpServers": {
    "exfer": {
      "command": "uvx",
      "args": ["exfer-mcp==0.2.2"],
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
- Auto-downloaded walletd binaries are run **only** after their SHA-256 matches a digest **baked into this exfer-mcp release** — re-verified on every run, not just first download. The trust anchor is the PyPI package's Trusted-Publishing provenance, **not** the mutable GitHub release (a co-located `SHA256SUMS` would be worthless against a release/account compromise). A mismatch or an unpinned walletd version is refused; `EXFER_WALLETD_BIN` overrides with a binary you built/trust.

## License

MIT
