"""Runtime configuration pulled from the process environment.

The MCP host (Claude Desktop, Claude Code, …) launches this server as
a subprocess with whatever env it was configured with. We never parse
CLI flags; the only knobs are env vars so the host config can wire
them in one place.

Required:

* ``WALLETD_URL`` — base URL of the walletd JSON-RPC endpoint.
* ``WALLETD_AUTH_TOKEN`` — bearer token walletd was started with.

Optional:

* ``WALLETD_FINGERPRINT`` — SHA-256 fingerprint of walletd's TLS cert
  when the URL is ``https://``. Required for https URLs.
* ``EXFER_MCP_DEFAULT_FEE_RATE`` — fee_rate (exfers/byte) to pass to
  spending tools when the agent doesn't specify one. Default unset →
  walletd's own default (1 exfer/byte).
* ``EXFER_MCP_HTTPX_TIMEOUT`` — per-RPC timeout in seconds. Default 30.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    """Raised at startup when a required env var is missing or invalid.

    We surface this distinctly from a runtime walletd error so the MCP
    host can render "fix your config" instead of "walletd is broken".
    """


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
                "(e.g. `\"WALLETD_URL\": \"http://127.0.0.1:7448\"`)"
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
            raise ConfigError(
                f"EXFER_MCP_HTTPX_TIMEOUT must be a number, got {timeout_str!r}"
            ) from exc
        if httpx_timeout <= 0:
            raise ConfigError(
                f"EXFER_MCP_HTTPX_TIMEOUT must be positive, got {httpx_timeout}"
            )

        return cls(
            walletd_url=url,
            walletd_token=token,
            walletd_fingerprint=fingerprint,
            default_fee_rate=default_fee_rate,
            httpx_timeout=httpx_timeout,
        )
