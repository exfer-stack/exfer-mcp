# Security

`exfer-mcp` lets an AI agent spend from an Exfer mainnet wallet **with no
per-transaction human approval**. Anything that can reach the MCP server — the
agent, or a prompt-injection of it — can move the funds. Treat it accordingly.

## Trust model

- **Public, auditable source** — this repo, the [`exfer-walletd`](https://github.com/exfer-stack/exfer-walletd)
  daemon it spawns, and the [`exfer`](https://github.com/exfer-stack/exfer-py) SDK
  it depends on. Read what you run before you fund it.
- **Release provenance** — every release is published *from this repo* via PyPI
  Trusted Publishing (OIDC, no long-lived token). The
  [PyPI page](https://pypi.org/project/exfer-mcp/) carries a signed attestation
  tying each artifact to this repository and its release workflow. Pin a version
  (`exfer-mcp==<x.y.z>`) so you run a specific, reviewed release.
- **walletd binary verification** — in managed (zero-setup) mode, the prebuilt
  `exfer-walletd` binary is downloaded only if its SHA-256 matches a digest
  **baked into this package** (`_PINNED_SHA256` in `walletd_fetch.py`), and the
  cached copy is re-verified on every run. The trust anchor is this package — not
  the mutable GitHub release / a co-located `SHA256SUMS`. Set `EXFER_WALLETD_BIN`
  to run a binary you built yourself instead.

If you cannot independently confirm this package is the one you intend to run
(for example, via a link from the project's own site/repos), **do not fund it**.

## Operating safely

- Keep only a small float. Bound the blast radius with walletd spend caps
  (`WALLETD_SPEND_CAP_PER_TX`, `WALLETD_SPEND_CAP_PER_PERIOD` +
  `WALLETD_SPEND_CAP_PERIOD_SECS`).
- `WALLETD_KEYSTORE_PASSPHRASE` is stored in **plaintext** in your MCP host's
  config file — treat that file as a wallet secret (`chmod 600`).
- On first run the managed wallet writes its 24-word recovery phrase to
  `<WALLETD_DATADIR>/RECOVERY_PHRASE.txt` (mode `0600`). Copy it offline, then
  delete the file. Losing **both** the phrase and the passphrase makes the wallet
  unrecoverable.
- No per-call human approval is built in — that is the MCP host's job.

## Reporting a vulnerability

Please report security issues **privately**, not as public issues:

- Open a private advisory via this repo's **GitHub Security Advisories**, or
- Contact the maintainers listed at https://exfer.info.

We aim to acknowledge reports within a few days.
