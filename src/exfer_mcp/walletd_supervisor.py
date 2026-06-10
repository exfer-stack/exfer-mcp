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
import ctypes
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

# How many recent walletd log lines to retain for diagnosing a startup exit.
_LOG_TAIL_LINES = 14

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


# --- orphan-cleanup primitives ---------------------------------------------
#
# Managed mode runs one walletd per datadir. Three reinforcing mechanisms keep
# a walletd from outliving its MCP server and wedging the next session on the
# datadir's DB lock:
#   1. PR_SET_PDEATHSIG — the kernel SIGKILLs walletd when the server dies,
#      even on an un-catchable SIGKILL where atexit/signal cleanup can't run.
#   2. a pid-file reap on bring-up — kills any walletd left holding *our*
#      datadir by a prior hard-killed session (covers the residual window and
#      pre-existing orphans).
#   3. ensure_ready() retry — relaunches a fresh bring-up after a failure, so a
#      cleared cause (or a spurious pdeathsig kill) self-heals.
_PR_SET_PDEATHSIG = 1
_LIBC: ctypes.CDLL | None
try:
    _LIBC = ctypes.CDLL("libc.so.6", use_errno=True)
except OSError:  # pragma: no cover - non-glibc / non-Linux
    _LIBC = None

# Name of the file (under the datadir) recording the managed walletd's pid.
_PIDFILE_NAME = "walletd-mcp.pid"


def _set_pdeathsig() -> None:  # pragma: no cover - runs in the forked child
    """preexec_fn: ask the kernel to SIGKILL this child if its parent dies."""
    if _LIBC is not None:
        _LIBC.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL)


def _proc_cmdline(pid: int) -> str | None:
    """Return ``pid``'s argv (NULs→spaces) via /proc, or None if not readable.

    None means the pid is dead, unreadable, or /proc is absent (non-Linux) —
    in every such case the reap treats it as "nothing to kill".
    """
    if pid <= 1:
        return None
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (FileNotFoundError, OSError, ProcessLookupError):
        return None
    return raw.replace(b"\0", b" ").decode("utf-8", "replace")


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
        else:
            self._token = token
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
            _eprint("[walletd] previous bring-up failed — retrying managed walletd startup...")
            self._ready_error = None
            self._token = None
            self._proc = None  # the failed attempt's walletd is already dead
            new_event = asyncio.Event()
            self._ready_event = new_event
        self._bringup_task = asyncio.create_task(self._bring_up(self.choose_bind()))
        return new_event

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

    def _pidfile(self) -> Path:
        return self._config.datadir / _PIDFILE_NAME

    def _reap_stale_walletd(self) -> None:
        """Kill a walletd left holding our datadir by a prior hard-killed run.

        Reads the pid we recorded last time (``<datadir>/walletd-mcp.pid``)
        and SIGKILLs it ONLY if it is still a live ``exfer-walletd`` whose
        argv references *this* datadir — so PID reuse, a dead pid, or another
        datadir's walletd are all left untouched. Without this, an orphan from
        a SIGKILLed session keeps the redb lock and every later bring-up fails
        with "Database already open".
        """
        try:
            raw = self._pidfile().read_text().strip()
        except (FileNotFoundError, OSError):
            return
        if not raw.isdigit():
            return
        pid = int(raw)
        cmd = _proc_cmdline(pid)
        if cmd is None or "exfer-walletd" not in cmd or str(self._config.datadir) not in cmd:
            return
        _eprint(
            f"[walletd] reaping orphaned walletd pid={pid} holding {self._config.datadir} "
            "(left by a prior hard-killed session)"
        )
        with contextlib.suppress(ProcessLookupError, OSError):
            os.kill(pid, signal.SIGKILL)
        # Wait (up to ~3s) for the OS to release the datadir's DB lock.
        for _ in range(60):
            if _proc_cmdline(pid) is None:
                break
            time.sleep(0.05)

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
                # Linux: have the kernel SIGKILL walletd if this server dies —
                # the orphan-proofing that survives an un-catchable SIGKILL.
                # No-op where unsupported (preexec only runs on POSIX; the
                # prctl itself no-ops without glibc).
                preexec_fn=_set_pdeathsig if os.name == "posix" else None,
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

        # Record the pid so the NEXT bring-up (this session or a future one)
        # can reap this walletd if we are hard-killed before stop() runs.
        with contextlib.suppress(OSError):
            self._pidfile().write_text(str(proc.pid))

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
