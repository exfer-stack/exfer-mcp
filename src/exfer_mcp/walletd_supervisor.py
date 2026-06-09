"""Managed-walletd supervisor.

In MANAGED mode (``WALLETD_URL`` unset) ``exfer-mcp`` is self-contained:
instead of connecting to an externally-run ``exfer-walletd``, it spawns
and supervises its own walletd subprocess — the same way the browser MCP
manages its own headless browser. The MCP host only has to set
``WALLETD_KEYSTORE_PASSPHRASE`` (and ``EXFER_WALLETD_BIN`` if walletd
isn't on ``PATH``) and everything else "just works" against the project's
public mainnet reference node + indexer.

Lifecycle (all driven from :meth:`WalletdSupervisor.start` /
:meth:`WalletdSupervisor.stop`):

1. Resolve a free loopback bind (default ``127.0.0.1:7448``; if busy,
   pick a free ephemeral loopback port so a managed walletd never
   collides with a user-run one).
2. If the datadir has no keystore, run ``<bin> init-seeded`` and surface
   the generated recovery phrase PROMINENTLY to stderr — it is the user's
   only backup. If a keystore already exists, skip init.
3. Spawn ``<bin> --datadir … --bind … --node-rpc … [--indexer-rpc …]``
   with the passphrase in env, forwarding its stdout/stderr to the MCP's
   stderr with a ``[walletd]`` prefix.
4. Poll ``get_block_height`` against the bind until it answers (or a
   timeout), then read the bearer token from ``<datadir>/token-spend``.
5. Hand back the effective URL + token so the rest of exfer-mcp uses the
   spawned instance unchanged.
6. On shutdown (atexit + SIGINT/SIGTERM + a ``finally`` around the server
   run) terminate the subprocess — SIGTERM, then SIGKILL after a grace
   period. Idempotent; no orphaned walletd processes.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from types import FrameType

from .config import ConfigError, ManagedConfig

# typeshed's signal-handler type: a Python callable, one of the special
# SIG_* int dispositions, or None.
_SignalHandler = Callable[[int, "FrameType | None"], object] | int | None

# walletd writes three scoped bearer tokens on first run. The MCP tool
# surface includes spending tools (exfer_transfer, htlc_lock, …) so we
# use the broadest scope; the spend token can also serve read calls.
_TOKEN_FILENAME = "token-spend"

# How long to wait for the spawned walletd to answer a health probe
# before giving up. The follower does a cold index scan on first start,
# but the JSON-RPC surface (get_block_height) comes up well before that.
_READY_TIMEOUT_SECS = 30.0
_READY_POLL_INTERVAL_SECS = 0.25

# Grace period between SIGTERM and SIGKILL on shutdown.
_TERM_GRACE_SECS = 5.0

# walletd prints its three scoped bearer tokens (read / manage / SPEND) in
# plaintext on first-run init. Each is 64 lowercase hex chars. We forward
# walletd's stdout/stderr to the MCP host's stderr (which hosts like Claude
# Desktop persist to durable log files), so we MUST redact any token before
# it lands in a log: the spend token grants full hot-wallet spend authority.
# Match a run of 64 hex chars not flanked by other hex chars (so we don't
# clip block hashes mid-word — those are also surfaced, but a 64-hex token
# on its own is what leaks). 32 lead chars are kept for debuggability.
_HEX_TOKEN_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])")
_TOKEN_KEEP_PREFIX = 8


def _keystore_exists(datadir: Path) -> bool:
    """True if ``datadir`` already holds an initialised keystore.

    ``init-seeded`` writes ``wallets/seed.enc``; a plain (non-seeded)
    keystore would still have ``wallets/state.json``. Either marks an
    existing keystore we must not re-init (that would overwrite the
    user's only key material).
    """
    wallets = datadir / "wallets"
    return (wallets / "seed.enc").exists() or (wallets / "state.json").exists()


def find_free_loopback_port(preferred: int, host: str = "127.0.0.1") -> int:
    """Return ``preferred`` if free on ``host``, else a free ephemeral port.

    A managed walletd must never collide with a user-run one bound to the
    conventional 7448. We probe the preferred port first; if it's taken we
    ask the OS for any free loopback port (bind to :0 and read it back).
    """
    if _port_is_free(host, preferred):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, 0))
        return int(s.getsockname()[1])


def _port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
        return True


def _eprint(msg: str) -> None:
    """Write a line to the MCP's stderr (the host surfaces it to the operator)."""
    print(msg, file=sys.stderr, flush=True)


def _redact_secrets(line: str) -> str:
    """Mask any 64-hex bearer token in a walletd log line before forwarding.

    walletd prints its scoped read/manage/SPEND tokens in plaintext on
    first-run init; forwarding them verbatim to the host's stderr (which
    Claude Desktop persists to durable log files) would leak full
    hot-wallet spend authority. We replace every 64-hex run with a short
    keep-prefix + ``…REDACTED`` so the line stays useful for debugging but
    carries no usable secret. Block hashes are also 64-hex and get masked
    too; that's acceptable — a forwarded log line is not where you should
    be reading hashes, and erring toward redaction is the safe default.
    """

    def _mask(match: re.Match[str]) -> str:
        tok = match.group(0)
        return f"{tok[:_TOKEN_KEEP_PREFIX]}…REDACTED"

    return _HEX_TOKEN_RE.sub(_mask, line)


class WalletdSupervisor:
    """Spawns and supervises a managed ``exfer-walletd`` subprocess.

    Construct from a :class:`~.config.ManagedConfig`; call
    :meth:`start` to (optionally init and) spawn walletd and return the
    effective ``(url, token)``; :meth:`stop` to tear it down. ``stop`` is
    idempotent and also wired into ``atexit`` + SIGINT/SIGTERM by
    :meth:`start`, so an abrupt exit never leaves an orphan.
    """

    def __init__(self, config: ManagedConfig) -> None:
        self._config = config
        self._proc: subprocess.Popen[bytes] | None = None
        self._log_thread: threading.Thread | None = None
        self._stopped = False
        self._lock = threading.Lock()
        self._port: int | None = None
        self._prev_sigterm: _SignalHandler = None
        self._prev_sigint: _SignalHandler = None

    @property
    def bind(self) -> str:
        """The chosen ``host:port`` (only valid after :meth:`start`)."""
        if self._port is None:
            raise RuntimeError("bind not chosen yet; call start() first")
        return f"{self._config.bind_host}:{self._port}"

    def start(self) -> tuple[str, str]:
        """Init (if needed), spawn walletd, wait for ready, return (url, token).

        Raises :class:`~.config.ConfigError` for operator-fixable problems
        (binary not found, init failed, never became ready) so the host
        renders "fix your config" rather than "walletd is broken".
        """
        if self._proc is not None:
            raise RuntimeError(
                "WalletdSupervisor.start() called twice; a walletd subprocess is "
                "already running for this supervisor"
            )
        cfg = self._config
        self._port = find_free_loopback_port(cfg.bind_port, cfg.bind_host)
        bind = self.bind

        self._init_keystore_if_needed()
        self._install_signal_handlers()
        atexit.register(self.stop)

        self._spawn(bind)
        url = f"http://{bind}"
        token = self._wait_until_ready(url)
        _eprint(f"[walletd] ready at {url} (managed, datadir={cfg.datadir})")
        return url, token

    # -- init ------------------------------------------------------------

    def _init_keystore_if_needed(self) -> None:
        cfg = self._config
        cfg.datadir.mkdir(parents=True, exist_ok=True)
        if _keystore_exists(cfg.datadir):
            _eprint(f"[walletd] reusing existing keystore at {cfg.datadir}")
            return

        _eprint(f"[walletd] no keystore at {cfg.datadir} — initialising a new seeded wallet")
        env = self._child_env()
        try:
            completed = subprocess.run(
                [cfg.binary, "--datadir", str(cfg.datadir), "init-seeded"],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ConfigError(
                f"EXFER_WALLETD_BIN points at a binary that does not exist: {cfg.binary!r}"
            ) from exc

        if completed.returncode != 0:
            raise ConfigError(
                "walletd init-seeded failed "
                f"(exit {completed.returncode}). stderr:\n{completed.stderr.strip()}"
            )

        mnemonic = self._extract_mnemonic(completed.stdout)
        self._announce_mnemonic(mnemonic, completed.stdout)

    @staticmethod
    def _extract_mnemonic(stdout: str) -> str | None:
        """Pull the recovery phrase out of init-seeded's JSON stdout."""
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            phrase = payload.get("mnemonic")
            if isinstance(phrase, str) and phrase:
                return phrase
        return None

    def _announce_mnemonic(self, mnemonic: str | None, raw_stdout: str) -> None:
        """Surface the recovery phrase PROMINENTLY — it's the only backup."""
        bar = "=" * 72
        _eprint("")
        _eprint(bar)
        _eprint("  BACK UP THIS RECOVERY PHRASE")
        _eprint("  This managed wallet is a HOT WALLET. The 24-word phrase below is")
        _eprint("  the ONLY way to recover its funds. exfer-mcp will not show it again.")
        _eprint(bar)
        if mnemonic:
            _eprint("")
            _eprint(f"  {mnemonic}")
            _eprint("")
        else:
            # Never swallow it: if parsing failed, dump walletd's raw output.
            _eprint("  (could not parse the phrase — raw walletd init output follows)")
            for line in raw_stdout.splitlines():
                _eprint(f"  {line}")
        _eprint(bar)
        _eprint("")

    # -- spawn -----------------------------------------------------------

    def _build_argv(self, bind: str) -> list[str]:
        cfg = self._config
        argv = [
            cfg.binary,
            "--datadir",
            str(cfg.datadir),
            "--bind",
            bind,
            "--node-rpc",
            cfg.node_rpc,
        ]
        if cfg.indexer_rpc:
            argv += ["--indexer-rpc", cfg.indexer_rpc]
        return argv

    def _child_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["WALLETD_KEYSTORE_PASSPHRASE"] = self._config.keystore_passphrase
        return env

    def _spawn(self, bind: str) -> None:
        argv = self._build_argv(bind)
        _eprint(f"[walletd] spawning: {' '.join(argv)}")
        try:
            self._proc = subprocess.Popen(
                argv,
                env=self._child_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                # New process group so our SIGINT (Ctrl-C in a TTY) doesn't
                # also hit the child out-of-band; we deliver signals to it
                # ourselves in stop().
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise ConfigError(
                f"EXFER_WALLETD_BIN points at a binary that does not exist: {self._config.binary!r}"
            ) from exc

        self._log_thread = threading.Thread(
            target=self._forward_logs, args=(self._proc,), daemon=True
        )
        self._log_thread.start()

    @staticmethod
    def _forward_logs(proc: subprocess.Popen[bytes]) -> None:
        """Forward walletd's combined output to our stderr with a prefix.

        Every line is run through :func:`_redact_secrets` first so a
        first-run bearer token (printed in plaintext by walletd) never
        reaches the host's durable stderr log.
        """
        stream = proc.stdout
        if stream is None:
            return
        with contextlib.suppress(ValueError, OSError):
            for raw in iter(stream.readline, b""):
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                _eprint(f"[walletd] {_redact_secrets(line)}")

    # -- readiness -------------------------------------------------------

    def _wait_until_ready(self, url: str) -> str:
        """Poll get_block_height until walletd answers; return the spend token.

        We can't read the token before walletd writes it (first run), so we
        re-read the token file on each poll and only treat the probe as a
        success once it returns a JSON-RPC result.
        """
        cfg = self._config
        token_path = cfg.datadir / _TOKEN_FILENAME
        deadline = time.monotonic() + _READY_TIMEOUT_SECS
        last_err: str = "no response"

        while time.monotonic() < deadline:
            # walletd died during startup — surface its exit immediately.
            if self._proc is not None and self._proc.poll() is not None:
                raise ConfigError(
                    f"managed walletd exited during startup (code {self._proc.returncode}); "
                    "see the [walletd] log lines above"
                )
            token = self._read_token(token_path)
            if token is not None:
                ok, last_err = self._probe(url, token)
                if ok:
                    return token
            time.sleep(_READY_POLL_INTERVAL_SECS)

        # Timed out — tear down the half-started child so we don't orphan it.
        self.stop()
        raise ConfigError(
            f"managed walletd did not become ready within {_READY_TIMEOUT_SECS:.0f}s "
            f"(last health probe: {last_err}). Check the [walletd] log above — common "
            "causes are an unreachable EXFER_NODE_RPC or a wrong WALLETD_KEYSTORE_PASSPHRASE."
        )

    @staticmethod
    def _read_token(path: Path) -> str | None:
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return token or None

    @staticmethod
    def _probe(url: str, token: str) -> tuple[bool, str]:
        """One get_block_height JSON-RPC probe. Returns (ok, error_detail)."""
        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "get_block_height", "params": []}
        ).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {token}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError) as exc:
            return False, str(exc)
        except json.JSONDecodeError as exc:
            return False, f"bad JSON: {exc}"
        if "result" in payload:
            return True, ""
        return False, json.dumps(payload.get("error", payload))

    # -- shutdown --------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        """Tear down walletd on SIGINT/SIGTERM, then chain to the prior handler."""

        def handler(signum: int, frame: FrameType | None) -> None:
            self.stop()
            prev = self._prev_sigint if signum == signal.SIGINT else self._prev_sigterm
            if callable(prev):
                prev(signum, frame)
            elif prev == signal.SIG_DFL:
                # Re-raise with the default disposition so exit codes are sane.
                signal.signal(signum, signal.SIG_DFL)
                os.kill(os.getpid(), signum)

        with contextlib.suppress(ValueError):
            # signal.signal raises ValueError off the main thread; that's fine,
            # atexit + the finally in server.py still cover those paths.
            self._prev_sigint = signal.signal(signal.SIGINT, handler)
            self._prev_sigterm = signal.signal(signal.SIGTERM, handler)

    def stop(self) -> None:
        """Terminate the walletd subprocess. Idempotent; no orphans.

        SIGTERM first; if it hasn't exited after a grace period, SIGKILL.
        Safe to call multiple times and from atexit / signal handlers.
        """
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            proc = self._proc

        if proc is None or proc.poll() is not None:
            return

        with contextlib.suppress(ProcessLookupError, OSError):
            proc.terminate()
        try:
            proc.wait(timeout=_TERM_GRACE_SECS)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=_TERM_GRACE_SECS)
        _eprint("[walletd] managed walletd stopped")
