# Changelog

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
