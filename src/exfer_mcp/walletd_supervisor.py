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

import asyncio
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
from collections import deque
from collections.abc import Callable
from pathlib import Path
from types import FrameType

import psutil

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

# Retry pacing: after the first (free) retry, back off so a permanent cause
# (unreachable node, etc.) can't make every tool call respawn walletd.
_RETRY_BACKOFF_BASE_SECS = 5.0
_RETRY_BACKOFF_MAX_SECS = 60.0

# How many recent walletd log lines to retain for diagnosing a startup exit.
_LOG_TAIL_LINES = 14

# walletd prints its three scoped bearer tokens (read / manage / SPEND) in
# plaintext on first-run init. Each is 64 lowercase hex chars. We forward
# walletd's stdout/stderr to the MCP host's stderr (which hosts like Claude
# Desktop persist to durable log files), so we MUST redact any token before
# it lands in a log: the spend token grants full spend authority over the wallet.
# Match a run of 64 hex chars not flanked by other hex chars (so we don't
# clip block hashes mid-word — those are also surfaced, but a 64-hex token
# on its own is what leaks). 32 lead chars are kept for debuggability.
_HEX_TOKEN_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])")
_TOKEN_KEEP_PREFIX = 8


def _seeded_keystore_exists(datadir: Path) -> bool:
    """True if a SEEDED managed keystore (``wallets/seed.enc``) is present.

    ``init-seeded`` writes ``wallets/seed.enc``. A bare ``wallets/state.json``
    with NO ``seed.enc`` is a *seedless* keyring (an external/desktop walletd
    touched this datadir) — deliberately NOT counted as seeded: treating it as
    "initialised" would skip our seed init and leave a wallet with no recovery
    phrase whose ``generate_address`` fails. That case is rejected explicitly in
    :meth:`_init_keystore_if_needed`.
    """
    return (datadir / "wallets" / "seed.enc").exists()


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
    full spend authority over the wallet. We replace every 64-hex run with a short
    keep-prefix + ``…REDACTED`` so the line stays useful for debugging but
    carries no usable secret. Block hashes are also 64-hex and get masked
    too; that's acceptable — a forwarded log line is not where you should
    be reading hashes, and erring toward redaction is the safe default.
    """

    def _mask(match: re.Match[str]) -> str:
        tok = match.group(0)
        return f"{tok[:_TOKEN_KEEP_PREFIX]}…REDACTED"

    return _HEX_TOKEN_RE.sub(_mask, line)


# --- orphan-cleanup primitives ---------------------------------------------
#
# Managed mode runs one walletd per datadir. Two cross-platform mechanisms keep
# a walletd from outliving its MCP server and wedging the next session on the
# datadir's DB lock:
#   1. a PARENT-AWARE process-scan reap on bring-up (psutil) — SIGKILL an
#      exfer-walletd on THIS datadir whose MCP-server parent has died (a true
#      orphan from a hard-killed session); a walletd with a LIVE parent (a
#      concurrent session) is left alone, and our own spawn then fails with a
#      clear "already in use" message rather than killing someone else's wallet.
#   2. ensure_ready() retry — relaunches a fresh bring-up after a failure so a
#      cleared cause (reaped orphan) self-heals.
#
# (An earlier build armed PR_SET_PDEATHSIG, but it fires on the death of the
# *thread* that forked walletd — the asyncio worker-pool thread here — not the
# process, so it could SIGKILL a live walletd mid-session. Dropped in favour of
# the parent-aware reap, which is correct on every platform.)


def _parent_alive(ppid: int | None) -> bool:
    """True if ``ppid`` is a live, non-init process — a plausible live owner.

    An orphaned walletd has reparented to init (ppid 1) or its parent pid is
    gone; either way this returns False and the reap may claim it. A walletd
    whose parent is still alive belongs to a concurrent session and is spared.
    """
    if not ppid or ppid <= 1:
        return False
    return psutil.pid_exists(ppid)


def _argv_targets_datadir(cmdline: list[str], datadir: Path) -> bool:
    """True if any argv element resolves (realpath) to ``datadir``.

    Matched by realpath, not string equality, so a trailing slash / symlink /
    relative spelling in the orphan's launch args can't defeat the match.
    """
    target = os.path.realpath(datadir)
    for arg in cmdline:
        if not arg or arg.startswith("-"):
            continue
        try:
            if os.path.realpath(arg) == target:
                return True
        except (OSError, ValueError):
            continue
    return False


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
        self._init_proc: subprocess.Popen[str] | None = None
        self._log_thread: threading.Thread | None = None
        self._log_tail: deque[str] = deque(maxlen=_LOG_TAIL_LINES)
        self._stopped = False
        self._lock = threading.Lock()
        self._port: int | None = None
        self._prev_sigterm: _SignalHandler = None
        self._prev_sigint: _SignalHandler = None
        # -- lazy (managed, non-blocking) machinery --------------------
        # Set once start_background() schedules the bring-up task; the
        # background task resolves _ready_event when walletd answers a
        # health probe (and records the spend token), or records
        # _ready_error if bring-up fails. ensure_ready() awaits this.
        self._ready_event: asyncio.Event | None = None
        self._ready_error: BaseException | None = None
        self._token: str | None = None
        self._bringup_task: asyncio.Task[None] | None = None
        self._bringup_started = False
        # Retry pacing (ensure_ready): consecutive bring-up failures + when the
        # last one happened, so a permanent cause can't respawn-storm walletd.
        self._failure_count = 0
        self._last_failure_at = 0.0

    @property
    def bind(self) -> str:
        """The chosen ``host:port`` (only valid after the port is chosen)."""
        if self._port is None:
            raise RuntimeError("bind not chosen yet; call choose_bind()/start() first")
        return f"{self._config.bind_host}:{self._port}"

    # -- lazy / non-blocking startup (managed mode) ----------------------

    def choose_bind(self) -> str:
        """Pick the loopback bind SYNCHRONOUSLY and return the walletd URL.

        Instant — only finds a free port (no init, no spawn, no probe). The
        URL is known immediately so the effective Config (and thus the MCP
        handshake + list_tools) can be built with ZERO wait, while the
        actual walletd boot happens later in the background.

        Idempotent: a second call returns the already-chosen URL.
        """
        if self._port is None:
            cfg = self._config
            self._port = find_free_loopback_port(cfg.bind_port, cfg.bind_host)
        return f"http://{self.bind}"

    def start_background(self) -> str:
        """Choose the bind, then SPAWN walletd in the background; return the URL.

        Does NOT wait for readiness — schedules an asyncio task that runs
        init-if-needed → spawn → poll-health → read-token and sets the
        readiness event when walletd answers. The caller (the MCP server)
        can start serving immediately; the handshake never blocks on this.

        Must be called from within a running event loop. Idempotent: a
        repeat call is a no-op and returns the same URL (guards against
        double-start, mirroring the sync :meth:`start`).
        """
        url = self.choose_bind()
        if self._bringup_started:
            return url
        self._bringup_started = True

        self._install_signal_handlers()
        atexit.register(self.stop)

        self._ready_event = asyncio.Event()
        _eprint(f"[walletd] starting (managed, datadir={self._config.datadir})...")
        self._bringup_task = asyncio.create_task(self._bring_up(url))
        return url

    async def _bring_up(self, url: str) -> None:
        """Background task: init (if needed) → spawn → health-poll → token.

        Runs the blocking steps (Argon2 keystore init via subprocess.run,
        the spawn, and the health poll) in a worker thread so the event
        loop — and therefore the MCP handshake / list_tools — stays
        responsive. Records the spend token + sets the ready event on
        success; records the error on failure (ensure_ready re-raises it).
        """
        # Capture the event this attempt owns: a retry (relaunch) swaps in a
        # fresh _ready_event, and we must resolve the one we were launched for.
        event = self._ready_event
        assert event is not None
        try:
            token = await asyncio.to_thread(self._bring_up_blocking, url)
        except Exception as exc:
            # Record every failure (ConfigError on timeout/spawn-fail, or an
            # unexpected error) so ensure_ready() can re-raise it as a clear
            # tool error instead of hanging. CancelledError (a BaseException)
            # is deliberately NOT caught — it propagates to cancel the task.
            self._ready_error = exc
            self._failure_count += 1
            self._last_failure_at = time.monotonic()
        else:
            self._token = token
            self._failure_count = 0
            _eprint(f"[walletd] ready at {url} (managed)")
        finally:
            event.set()

    def _bring_up_blocking(self, url: str) -> str:
        """The synchronous bring-up body (run off-loop via a worker thread)."""
        # If stop() already ran (server exited before ready), don't spawn.
        with self._lock:
            if self._stopped:
                raise ConfigError("managed walletd bring-up aborted: server is shutting down")
        # Clear any orphan from a prior hard-killed session before we touch
        # the datadir, so its DB lock can't fail our spawn.
        self._reap_stale_walletd()
        self._init_keystore_if_needed()
        # Re-check after the (potentially slow) init: a teardown may have
        # landed while we were unlocking the keystore.
        with self._lock:
            if self._stopped:
                raise ConfigError("managed walletd bring-up aborted: server is shutting down")
        self._spawn(self.bind)
        return self._wait_until_ready(url)

    async def ensure_ready(self) -> tuple[str, str]:
        """Await walletd readiness; return ``(url, token)``. Idempotent.

        Returns immediately once walletd is up; otherwise awaits the
        in-progress background bring-up. Re-raises the bring-up error
        (e.g. :class:`~.config.ConfigError` on timeout / spawn failure) so
        a tool call returns a clear error instead of hanging forever.

        Must be called after :meth:`start_background`.
        """
        event = self._ready_event
        if event is None:
            raise RuntimeError("ensure_ready() called before start_background()")
        await event.wait()
        if self._ready_error is not None:
            # The previous attempt failed. Relaunch ONE fresh bring-up (the
            # reap may now clear a stale orphan, or whatever broke may have
            # since resolved) and await it; re-raise only if it fails too.
            event = self._relaunch_bringup(event)
            await event.wait()
            if self._ready_error is not None:
                raise self._ready_error
        assert self._token is not None
        return f"http://{self.bind}", self._token

    def _relaunch_bringup(self, failed_event: asyncio.Event) -> asyncio.Event:
        """Reset failed-bring-up state and schedule a fresh attempt; return the
        new event to await.

        Concurrency-safe: only the first caller observing ``failed_event``
        relaunches — a racing caller gets back whatever event is now current.
        Once :meth:`stop` has run we do not relaunch (return the failed event
        so the awaiter re-raises the recorded shutdown error).
        """
        with self._lock:
            if self._ready_event is not failed_event:
                assert self._ready_event is not None
                return self._ready_event
            if self._stopped:
                return failed_event
            # A permanent cause (e.g. wrong passphrase) can't be fixed by a fresh
            # spawn — fast-fail with the recorded error, never respawn.
            if self._error_is_permanent():
                return failed_event
            # Back off after the first (free) retry so a still-broken cause
            # (unreachable node, …) doesn't respawn walletd on every tool call.
            backoff = self._retry_backoff_secs()
            if backoff > 0.0 and (time.monotonic() - self._last_failure_at) < backoff:
                return failed_event
            _eprint("[walletd] previous bring-up failed — retrying managed walletd startup...")
            self._terminate_proc()  # don't orphan a half-live previous attempt
            self._ready_error = None
            self._token = None
            new_event = asyncio.Event()
            self._ready_event = new_event
        self._bringup_task = asyncio.create_task(self._bring_up(self.choose_bind()))
        return new_event

    def _error_is_permanent(self) -> bool:
        """True if the recorded bring-up error can't be fixed by respawning."""
        msg = str(self._ready_error or "").lower()
        return any(s in msg for s in ("passphrase", "could not be unlocked", "decryption failed"))

    def _retry_backoff_secs(self) -> float:
        """Backoff before the next respawn. First failure → 0 (retry at once);
        then linear growth capped at the max."""
        if self._failure_count <= 1:
            return 0.0
        return min(_RETRY_BACKOFF_BASE_SECS * (self._failure_count - 1), _RETRY_BACKOFF_MAX_SECS)

    def _terminate_proc(self) -> None:
        """Terminate + clear the current walletd handle (SIGTERM, then SIGKILL).

        Used before a relaunch so a previous attempt that spawned but never
        became ready is not orphaned by dropping the only reference to it.
        """
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=_TERM_GRACE_SECS)
        if proc.poll() is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()

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
        wallets = cfg.datadir / "wallets"
        if _seeded_keystore_exists(cfg.datadir):
            _eprint(f"[walletd] reusing existing seeded keystore at {cfg.datadir}")
            return
        if (wallets / "state.json").exists():
            # Populated but seedless — refuse rather than skip init (which would
            # leave a wallet with no recovery phrase and a broken generate_address).
            raise ConfigError(
                f"managed mode: {cfg.datadir} holds a seedless/foreign wallet "
                "(wallets/state.json but no seed.enc). Managed mode needs a seeded "
                "keystore — use a fresh WALLETD_DATADIR, or run that wallet via "
                "external mode (set WALLETD_URL)."
            )

        _eprint(f"[walletd] no keystore at {cfg.datadir} — initialising a new seeded wallet")
        env = self._child_env()
        # Tracked Popen (not subprocess.run) so stop() can terminate the
        # init-seeded child if the server exits mid-init (the Argon2 keystore
        # derivation is a 10-20s window). Keeps the "no orphan" guarantee on the
        # first-run path too.
        try:
            proc = subprocess.Popen(
                [cfg.binary, "--datadir", str(cfg.datadir), "init-seeded"],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError as exc:
            raise ConfigError(
                f"EXFER_WALLETD_BIN points at a binary that does not exist: {cfg.binary!r}"
            ) from exc

        with self._lock:
            if self._stopped:
                with contextlib.suppress(ProcessLookupError, OSError):
                    proc.terminate()
                raise ConfigError("walletd init aborted: supervisor stopped")
            self._init_proc = proc

        try:
            stdout, stderr = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
            proc.wait()
            raise ConfigError("walletd init-seeded timed out after 60s") from None
        finally:
            with self._lock:
                self._init_proc = None

        if proc.returncode != 0:
            raise ConfigError(
                f"walletd init-seeded failed (exit {proc.returncode}). stderr:\n{stderr.strip()}"
            )

        mnemonic = self._extract_mnemonic(stdout)
        self._announce_mnemonic(mnemonic, stdout)

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
        """Persist the recovery phrase to a 0600 file — it's the only backup.

        Hosts routinely swallow a managed server's stderr (Claude Desktop, GUI
        hosts), so we DO NOT print the phrase to the log channel — that both
        loses it and leaves it in plaintext host logs. Instead it is written to
        ``<datadir>/RECOVERY_PHRASE.txt`` (mode 0600); stderr gets a pointer, not
        the words. Only if the file can't be written do we fall back to stderr,
        so the phrase is never silently lost.
        """
        bar = "=" * 72
        body = mnemonic if mnemonic else raw_stdout.strip()
        phrase_path = self._config.datadir / "RECOVERY_PHRASE.txt"
        try:
            # 0600 from creation (not a chmod-after race).
            fd = os.open(str(phrase_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(
                    "EXFER managed wallet — recovery phrase.\n"
                    "This is the ONLY backup for this wallet's funds. Copy the words\n"
                    "below somewhere offline, then DELETE this file.\n\n"
                    f"{body}\n"
                )
            with contextlib.suppress(OSError):
                phrase_path.chmod(0o600)
        except OSError as exc:
            # Last resort only: file unwritable → surface to stderr so it's not lost.
            _eprint(bar)
            _eprint(f"  BACK UP THIS RECOVERY PHRASE (could not write {phrase_path}: {exc}).")
            _eprint("  It is the ONLY backup for this wallet:")
            _eprint(f"  {body}")
            _eprint(bar)
            return
        _eprint("")
        _eprint(bar)
        _eprint("  RECOVERY PHRASE SAVED — back up the only copy of this wallet")
        _eprint(f"  Written (mode 0600) to: {phrase_path}")
        _eprint("  Copy the 24 words offline, then DELETE that file.")
        _eprint("  (Not printed here — so it never lands in host logs.)")
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

    def _reap_stale_walletd(self) -> None:
        """SIGKILL a TRUE orphan walletd on our datadir; never a live peer.

        Managed mode runs one walletd per datadir. A walletd whose argv targets
        THIS datadir but whose MCP-server parent has died (reparented to init /
        gone) is an orphan from a hard-killed session — it keeps the redb lock
        and would fail every later bring-up with "Database already open", so we
        reap it. A walletd whose parent is still ALIVE belongs to a concurrent
        live session and is left alone (our spawn then fails with a clear
        "already in use" message instead of killing someone else's wallet).
        Portable via psutil; datadir matched by realpath so spelling/symlink
        differences don't defeat it.
        """
        datadir = self._config.datadir  # canonicalised in ManagedConfig.from_env
        own_pid = self._proc.pid if self._proc is not None else None
        reaped: list[psutil.Process] = []
        for proc in psutil.process_iter(["cmdline", "ppid"]):
            if proc.pid == own_pid:
                continue
            try:
                cmdline = proc.info.get("cmdline") or []
                ppid = proc.info.get("ppid")
            except (psutil.Error, OSError):
                continue
            if not any("exfer-walletd" in arg for arg in cmdline):
                continue
            if not _argv_targets_datadir(cmdline, datadir):
                continue
            if _parent_alive(ppid):
                _eprint(
                    f"[walletd] datadir {datadir} is held by a LIVE session "
                    f"(walletd pid={proc.pid}, parent pid={ppid}) — not reaping"
                )
                continue
            _eprint(
                f"[walletd] reaping orphaned walletd pid={proc.pid} on {datadir} "
                "(its MCP-server parent is gone)"
            )
            with contextlib.suppress(psutil.Error, OSError):
                proc.kill()
            reaped.append(proc)
        # Wait (up to ~3s each) for the OS to release the datadir's DB lock.
        for proc in reaped:
            with contextlib.suppress(psutil.Error, OSError):
                proc.wait(timeout=3)

    def _spawn(self, bind: str) -> None:
        argv = self._build_argv(bind)
        _eprint(f"[walletd] spawning: {' '.join(argv)}")
        try:
            proc = subprocess.Popen(
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

        # Publish the handle under the lock and, if a teardown already
        # landed (server exited before we got here), kill it right back so
        # the background spawn never becomes an orphan.
        with self._lock:
            stopped = self._stopped
            self._proc = proc
        if stopped:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=_TERM_GRACE_SECS)
            if proc.poll() is None:
                with contextlib.suppress(ProcessLookupError, OSError):
                    proc.kill()
            raise ConfigError("managed walletd bring-up aborted: server is shutting down")

        self._log_thread = threading.Thread(target=self._forward_logs, args=(proc,), daemon=True)
        self._log_thread.start()

    def _forward_logs(self, proc: subprocess.Popen[bytes]) -> None:
        """Forward walletd's combined output to our stderr with a prefix.

        Every line is run through :func:`_redact_secrets` first so a
        first-run bearer token (printed in plaintext by walletd) never
        reaches the host's durable stderr log. The redacted lines are also
        retained in a small ring buffer so a startup exit can be explained
        (see :meth:`_explain_startup_exit`).
        """
        stream = proc.stdout
        if stream is None:
            return
        with contextlib.suppress(ValueError, OSError):
            for raw in iter(stream.readline, b""):
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                redacted = _redact_secrets(line)
                self._log_tail.append(redacted)
                _eprint(f"[walletd] {redacted}")

    def _explain_startup_exit(self, code: int | None) -> str:
        """Map a walletd startup exit to a clear, actionable error.

        The common multi-session cause is that the managed datadir is already
        owned by another exfer-mcp instance, or its keystore was sealed with a
        different passphrase — walletd then exits before it can serve. Map the
        known signatures to a plain "wallet unavailable" message instead of a
        bare exit code, and append the recent ``[walletd]`` log tail.
        """
        tail = "\n".join(self._log_tail)
        low = tail.lower()
        datadir = self._config.datadir
        if any(s in low for s in ("decryption failed", "wrong passphrase", "keystore locked")):
            reason = (
                f"the managed wallet at {datadir} exists but could not be unlocked — "
                "WALLETD_KEYSTORE_PASSPHRASE does not match the passphrase that created it "
                "(another session likely created this wallet with a different passphrase)"
            )
        elif any(
            s in low
            for s in (
                "already in use",
                "in use by",
                "could not acquire",
                "lock",
                "resource temporarily unavailable",
                "address already in use",
                "database is locked",
            )
        ):
            reason = (
                f"the managed wallet at {datadir} is already in use by another exfer-mcp session"
            )
        else:
            reason = f"the managed walletd exited during startup (code {code})"
        return (
            f"managed wallet unavailable: {reason}. Managed mode uses one wallet per datadir and "
            "only one session can hold it at a time — for multiple Claude sessions give each a "
            "unique WALLETD_DATADIR, or run one shared walletd and use external mode "
            "(WALLETD_URL + WALLETD_AUTH_TOKEN)."
            + (f"\n--- recent [walletd] log ---\n{tail}" if tail else "")
        )

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
                raise ConfigError(self._explain_startup_exit(self._proc.returncode))
            token = self._read_token(token_path)
            if token is not None:
                ok, last_err = self._probe(url, token)
                if ok:
                    return token
            time.sleep(_READY_POLL_INTERVAL_SECS)

        # Timed out — tear down the half-started child so we don't orphan it.
        self.stop()
        node = self._config.node_rpc
        low = last_err.lower()
        node_unreachable = any(
            s in low
            for s in (
                "-32020",
                "upstream",
                "unreachable",
                "refused",
                "timed out",
                "timeout",
                "502",
                "503",
                "504",
                "no route",
                "getaddrinfo",
                "name resolution",
            )
        )
        if node_unreachable:
            hint = (
                f"the upstream Exfer node is unreachable from here (node_rpc={node}). "
                "Set EXFER_NODE_RPC to a node you can reach, or run one locally."
            )
        else:
            hint = (
                f"check the [walletd] log above — likely an unreachable node "
                f"(node_rpc={node}; override with EXFER_NODE_RPC) or a wrong "
                "WALLETD_KEYSTORE_PASSPHRASE."
            )
        raise ConfigError(
            f"managed walletd did not become ready within {_READY_TIMEOUT_SECS:.0f}s "
            f"(last health probe: {last_err}). {hint}"
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
            init_proc = self._init_proc

        # First-run init-seeded child (Argon2 window): terminate it too so a
        # stop mid-init leaves no lingering process.
        if init_proc is not None and init_proc.poll() is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                init_proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                init_proc.wait(timeout=_TERM_GRACE_SECS)
            with contextlib.suppress(ProcessLookupError, OSError):
                init_proc.kill()

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
