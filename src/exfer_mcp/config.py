"""Runtime configuration pulled from the process environment.

The MCP host (Claude Desktop, Claude Code, …) launches this server as
a subprocess with whatever env it was configured with. We never parse
CLI flags; the only knobs are env vars so the host config can wire
them in one place.

There are two ways to run, selected by whether ``WALLETD_URL`` is set:

**EXTERNAL mode** (``WALLETD_URL`` set) — the original behaviour. We
connect to an externally-run walletd:

* ``WALLETD_URL`` — base URL of the walletd JSON-RPC endpoint.
* ``WALLETD_AUTH_TOKEN`` — bearer token walletd was started with.
* ``WALLETD_FINGERPRINT`` — SHA-256 fingerprint of walletd's TLS cert
  when the URL is ``https://`` with a self-signed cert.

**MANAGED mode** (``WALLETD_URL`` unset) — exfer-mcp spawns + supervises
its own walletd, like the browser MCP manages its own browser:

* ``WALLETD_KEYSTORE_PASSPHRASE`` — REQUIRED; passed to walletd.
* ``EXFER_WALLETD_BIN`` — path to the walletd binary. Auto-detected on
  ``PATH`` (``exfer-walletd``) if unset.
* ``EXFER_NODE_RPC`` — upstream node(s); defaults to the public mainnet
  reference node + a backup.
* ``EXFER_INDEXER_RPC`` — indexer URL; defaults to the public mainnet
  indexer. Empty string disables indexer delegation.
* ``WALLETD_DATADIR`` — keystore + token dir; defaults to a stable
  managed dir so funds persist across restarts.
* ``EXFER_WALLETD_BIND`` — preferred ``host:port``; defaults to
  ``127.0.0.1:7448``, falling back to a free loopback port if busy.

Common to both modes:

* ``EXFER_MCP_DEFAULT_FEE_RATE`` — fee_rate (exfers/byte) to pass to
  spending tools when the agent doesn't specify one. Default unset →
  walletd's own default (1 exfer/byte).
* ``EXFER_MCP_HTTPX_TIMEOUT`` — per-RPC timeout in seconds. Default 30.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

# MANAGED-mode default endpoints — the project's PUBLISHED public community
# nodes (exfer-docs/nodes.toml: node-a/-b/-c), NOT any single app's private node
# (mobile/desktop ship their own infra IPs that aren't public). walletd
# round-robins + fails over across the comma-separated list, so we ship all
# three for resilience; ordered so the entries reachable from the widest set of
# networks come first (verified from multiple regions), so a typical run
# succeeds on the first try rather than relying on failover. Override with
# EXFER_NODE_RPC / EXFER_INDEXER_RPC. (Hardcoded IPs are inherently fragile —
# a DNS-named endpoint is the right long-term fix on the project side.)
DEFAULT_NODE_RPC = (
    "http://89.127.232.155:9334,http://80.78.31.82:9334,http://82.221.100.201:9334"
)
# Only node-a (82.221.100.201) also runs a public indexer; keep a second
# reachable indexer as failover for networks that can't reach it.
DEFAULT_INDEXER_RPC = "http://82.221.100.201:9335,http://64.176.231.198:9335"
DEFAULT_BIND = "127.0.0.1:7448"
DEFAULT_WALLETD_BIN = "exfer-walletd"
DEFAULT_DATADIR_NAME = ".exfer-walletd-mcp"


class ConfigError(RuntimeError):
    """Raised at startup when a required env var is missing or invalid.

    We surface this distinctly from a runtime walletd error so the MCP
    host can render "fix your config" instead of "walletd is broken".
    """


def _parse_common() -> tuple[int | None, float]:
    """Parse the mode-agnostic knobs (fee rate + httpx timeout)."""
    fee_rate_str = os.environ.get("EXFER_MCP_DEFAULT_FEE_RATE")
    default_fee_rate: int | None
    if fee_rate_str:
        try:
            default_fee_rate = int(fee_rate_str)
        except ValueError as exc:
            raise ConfigError(
                f"EXFER_MCP_DEFAULT_FEE_RATE must be an integer, got {fee_rate_str!r}"
            ) from exc
        if default_fee_rate <= 0:
            raise ConfigError(
                f"EXFER_MCP_DEFAULT_FEE_RATE must be positive, got {default_fee_rate}"
            )
    else:
        default_fee_rate = None

    timeout_str = os.environ.get("EXFER_MCP_HTTPX_TIMEOUT", "30")
    try:
        httpx_timeout = float(timeout_str)
    except ValueError as exc:
        raise ConfigError(f"EXFER_MCP_HTTPX_TIMEOUT must be a number, got {timeout_str!r}") from exc
    if httpx_timeout <= 0:
        raise ConfigError(f"EXFER_MCP_HTTPX_TIMEOUT must be positive, got {httpx_timeout}")
    return default_fee_rate, httpx_timeout


def managed_mode_selected() -> bool:
    """True when MANAGED mode applies — i.e. ``WALLETD_URL`` is unset/empty.

    A set ``WALLETD_URL`` always means EXTERNAL mode (unchanged); only an
    unset URL triggers the self-contained managed walletd.
    """
    return not os.environ.get("WALLETD_URL")


@dataclass(frozen=True)
class ManagedConfig:
    """MANAGED-mode knobs: where + how to spawn the supervised walletd."""

    binary: str
    keystore_passphrase: str
    node_rpc: str
    indexer_rpc: str | None
    datadir: Path
    bind_host: str
    bind_port: int

    @classmethod
    def from_env(cls) -> ManagedConfig:
        # Binary: explicit path, else auto-detect on PATH.
        binary = os.environ.get("EXFER_WALLETD_BIN")
        if binary:
            if not Path(binary).exists():
                raise ConfigError(
                    f"EXFER_WALLETD_BIN points at a binary that does not exist: {binary!r}"
                )
        else:
            found = shutil.which(DEFAULT_WALLETD_BIN)
            if found:
                binary = found
            else:
                # Zero-setup: fetch + verify a prebuilt walletd for this
                # platform (cached after first use). Lazy import avoids a
                # config <-> walletd_fetch cycle. Raises a clear ConfigError
                # if the binary can't be downloaded or verified.
                from .walletd_fetch import ensure_walletd_binary

                binary = str(ensure_walletd_binary())

        passphrase = os.environ.get("WALLETD_KEYSTORE_PASSPHRASE")
        if not passphrase:
            raise ConfigError(
                "managed mode requires WALLETD_KEYSTORE_PASSPHRASE — set it on the MCP "
                "host's `env` block so exfer-mcp can unlock (and on first run, create) "
                "the managed walletd keystore. "
                "(Or set WALLETD_URL to connect to an external walletd instead.)"
            )

        node_rpc = os.environ.get("EXFER_NODE_RPC") or DEFAULT_NODE_RPC

        # EXFER_INDEXER_RPC unset → default public indexer; explicit empty
        # string → caller deliberately disables indexer delegation.
        indexer_env = os.environ.get("EXFER_INDEXER_RPC")
        if indexer_env is None:
            indexer_rpc: str | None = DEFAULT_INDEXER_RPC
        else:
            indexer_rpc = indexer_env or None

        datadir_str = os.environ.get("WALLETD_DATADIR")
        # Canonicalise (absolute, symlinks/.. resolved) so the orphan reaper can
        # match the datadir reliably regardless of how it was spelled.
        datadir = (
            Path(datadir_str) if datadir_str else Path.home() / DEFAULT_DATADIR_NAME
        ).resolve()

        bind = os.environ.get("EXFER_WALLETD_BIND") or DEFAULT_BIND
        host, port = _parse_bind(bind)

        return cls(
            binary=binary,
            keystore_passphrase=passphrase,
            node_rpc=node_rpc,
            indexer_rpc=indexer_rpc,
            datadir=datadir,
            bind_host=host,
            bind_port=port,
        )


def _parse_bind(bind: str) -> tuple[str, int]:
    host, sep, port_str = bind.rpartition(":")
    if not sep:
        raise ConfigError(f"EXFER_WALLETD_BIND must be host:port, got {bind!r}")
    try:
        port = int(port_str)
    except ValueError as exc:
        raise ConfigError(f"EXFER_WALLETD_BIND port must be an integer, got {bind!r}") from exc
    if not 0 < port < 65536:
        raise ConfigError(f"EXFER_WALLETD_BIND port out of range, got {port}")
    return host or "127.0.0.1", port


@dataclass(frozen=True)
class Config:
    walletd_url: str
    walletd_token: str
    walletd_fingerprint: str | None
    default_fee_rate: int | None
    httpx_timeout: float

    @classmethod
    def from_env(cls) -> Config:
        url = os.environ.get("WALLETD_URL")
        token = os.environ.get("WALLETD_AUTH_TOKEN")
        if not url:
            raise ConfigError(
                "WALLETD_URL is unset — set it on the MCP host's `env` block "
                '(e.g. `"WALLETD_URL": "http://127.0.0.1:7448"`)'
            )
        if not token:
            raise ConfigError(
                "WALLETD_AUTH_TOKEN is unset — set it on the MCP host's "
                "`env` block to the bearer token walletd was started with"
            )

        # Fingerprint pinning is optional even for https. The SDK pins
        # only when set; without it, httpx falls back to the system CA
        # chain — appropriate for fly-fronted / public-CA TLS. The
        # operator path (walletd's own --tls with a self-signed cert)
        # SHOULD set the fingerprint, but enforcing it would block
        # legitimate publicly-fronted deployments.
        fingerprint = os.environ.get("WALLETD_FINGERPRINT") or None

        default_fee_rate, httpx_timeout = _parse_common()

        return cls(
            walletd_url=url,
            walletd_token=token,
            walletd_fingerprint=fingerprint,
            default_fee_rate=default_fee_rate,
            httpx_timeout=httpx_timeout,
        )

    @classmethod
    def for_managed(cls, url: str, token: str) -> Config:
        """Build the effective Config for a spawned (MANAGED) walletd.

        The supervisor returns the loopback URL + bearer token of the
        walletd it spawned; the rest of exfer-mcp then uses this Config
        exactly as it would an EXTERNAL one. No fingerprint (loopback
        http, no TLS); fee-rate + timeout come from the common knobs.
        """
        default_fee_rate, httpx_timeout = _parse_common()
        return cls(
            walletd_url=url,
            walletd_token=token,
            walletd_fingerprint=None,
            default_fee_rate=default_fee_rate,
            httpx_timeout=httpx_timeout,
        )
