# Changelog

## Unreleased

- **Address ergonomics + chain-height tools.** `exfer_generate_address`
  now returns `{address, pubkey, index}` as JSON (it previously returned
  only the address text and dropped the pubkey) — feed the `pubkey`
  straight into `exfer_quote_issue` as `payee_pubkey`. Two new read-only
  tools: `exfer_list_addresses` (returns the full per-address records)
  and `exfer_get_block_height` (returns `{height, block_id}` for the
  current chain tip). Tool surface 20 → 22. Requires the exfer-py SDK
  with the typed `generate_address`/`list_addresses` return shapes.
- **Managed-walletd mode.** exfer-mcp can now run self-contained: when
  `WALLETD_URL` is **unset**, it spawns and supervises its own
  `exfer-walletd` subprocess against the project's public mainnet
  reference node + indexer (overridable), instead of requiring an
  externally-run walletd. Set only `WALLETD_KEYSTORE_PASSPHRASE` (and
  `EXFER_WALLETD_BIN` if walletd isn't on `PATH`) and it "just works".
  On first run it initialises a seeded keystore and surfaces the 24-word
  recovery phrase to stderr. The managed walletd binds loopback only,
  picks a free port if the default 7448 is busy, and is terminated on
  every MCP exit path (atexit + SIGINT/SIGTERM) — no orphaned processes.
  Setting `WALLETD_URL` keeps the original **external** mode unchanged.
  New env: `WALLETD_KEYSTORE_PASSPHRASE`, `EXFER_WALLETD_BIN`,
  `EXFER_NODE_RPC`, `EXFER_INDEXER_RPC`, `WALLETD_DATADIR`,
  `EXFER_WALLETD_BIND`.

## 0.1.0

Initial release. Seven tools wrapping the `exfer-walletd` v1.9 Read +
Spend surface (via `exfer-walletd>=0.8.0`):

- `exfer_generate_address`
- `exfer_get_balance`
- `exfer_simulate_transfer`
- `exfer_transfer`
- `exfer_wait_for_tx`
- `exfer_payment_uri_encode`
- `exfer_payment_uri_decode`

stdio transport. Auth via `WALLETD_URL` + `WALLETD_AUTH_TOKEN` env
vars; optional TLS pinning via `WALLETD_FINGERPRINT`. Errors from
walletd (insufficient balance, wait-timeout, indexer-not-configured)
surface as MCP `isError=true` content with one-line plain-English
summaries so the agent can react instead of crashing the conversation.
