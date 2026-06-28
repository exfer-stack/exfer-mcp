# Changelog

## 0.7.0

- **Pool payout stats (read your mining pool's view).** One new read-only
  tool, `exfer_earn_pool_stats`, surfaces the pool-side account behind the
  `exfer_earn` miner: your ACCRUED (unpaid) balance in EXFER, the pool's
  PAYOUT THRESHOLD (`paymentThresholdCoins`, e.g. 100 EXFER), how much is
  left to reach it, your pool hashrate (current + average), online state,
  per-worker hashrate, payments count, and daily profit. This is the pool's
  *pending* balance, not your on-chain wallet balance. Works for both the
  shared PPLNS pool (`exfer-pplns`) and the SOLO pool (`exfer-solo`) via the
  `pool` arg. It queries the pool's HTTP stats API directly (not walletd or
  the node), defaulting to `https://api.ninjaraider.com` — the stats host of
  the default `exfer_earn` pool — overridable via `EXFER_MINE_POOL_API`
  (pool id via `EXFER_MINE_POOL_ID`, default `exfer-pplns`). Needs no
  walletd, so it is dispatched without the readiness gate. New module
  `exfer_mcp.tools.pool_stats`. Tool surface 39 → 40.
- **Network status + explorer tools (read the node directly).** Four new
  read-only tools that query the upstream Exfer L1 node's JSON-RPC surface
  directly — not walletd — so the agent ships with explorer-grade chain data
  beyond its own wallet:
  - `exfer_network_status` — the node's `get_node_info` (version, network,
    genesis_block_id, tip_height/tip_block_id, tip_age_seconds, peer_count,
    mempool_size/bytes, uptime_seconds, metrics).
  - `exfer_network_hashrate` — an honest, DERIVED estimate of whole-network
    hashrate: it sums each recent block's expected work
    (`2^256 / difficulty_target`) over a look-back window (default 60 blocks,
    overridable) and divides by the window's actual wall-clock span. Returns
    `{difficulty, est_hashrate_hs, work_per_block, target_block_seconds,
    window_blocks, window_seconds, tip_height, is_estimate}`. Unit is
    memory-hard Argon2id H/s (not comparable to a SHA-256 ASIC).
  - `exfer_get_block` — explorer block lookup by `height` or `block_id`.
  - `exfer_get_transaction` — explorer tx lookup by `tx_id` (mempool or
    confirmed), for ANY transaction, not just the wallet's.
  These need no walletd, so they are dispatched without the managed-walletd
  readiness gate and work even while walletd is booting or down. They honour
  the same `EXFER_NODE_RPC` (comma-separated, with failover) the managed
  walletd uses. New module `exfer_mcp.node_fetch` (a tiny async JSON-RPC node
  client). Tool surface 35 → 39.

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
