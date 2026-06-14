"""earn — give the agent a wallet that funds itself.

EXFER is CPU-mineable, so an autonomous agent can acquire its own starting
capital from nothing but compute — no human, no bank, no KYC, no funded account.
That is the one thing no card/stablecoin/x402 rail can offer: every one of those
requires a human-backed account to get the first unit in.

These tools run a lean, fee-free CPU miner (``exfer-agent-miner``) in the
background, pointed at a public EXFER stratum pool with the agent's own address
as the payout. The pool credits the agent for the work it contributes; payout
follows the pool's policy. Mining is fully independent of walletd, so these tools
need no wallet connection — the agent just supplies its address.

  exfer_earn(address)         -> start mining to that address
  exfer_earn_status()         -> hashrate / authorized / shares
  exfer_earn_stop()           -> stop the miner
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import time
from typing import Any

import mcp.types as mcp_types
from exfer import AsyncClient

from ..config import Config
from ._common import json_text, text

# A public EXFER stratum pool verified to accept this miner's shares. Override
# with EXFER_MINE_POOL or the `pool` argument; use exfer_earn_probe to check
# whether any given pool actually accepts shares before committing.
DEFAULT_POOL = os.environ.get("EXFER_MINE_POOL", "ssl://ninjaraider.com:44913")


def _resolve_miner_bin(explicit: str | None) -> str | None:
    """Find the exfer-agent-miner binary: explicit arg, env, then PATH."""
    candidate = explicit or os.environ.get("EXFER_AGENT_MINER_BIN")
    if candidate and os.path.exists(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return shutil.which("exfer-agent-miner")


class _Miner:
    """Module-level singleton supervising the background miner subprocess.

    One miner per MCP server. ``start`` spawns it; a pump task reads the
    miner's JSON stats line per interval off stdout into ``stats``; ``stop``
    terminates it.
    """

    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task[None] | None = None
        self.stats: dict[str, Any] = {}
        self.address: str | None = None
        self.pool: str | None = None
        self.started_at: float = 0.0

    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self, bin_path: str, address: str, pool: str, threads: int, gpu: str) -> None:
        argv = [
            bin_path,
            "--pool",
            pool,
            "--address",
            address,
            "--threads",
            str(threads),
            "--stats-secs",
            "5",
        ]
        if gpu:
            argv += ["--gpu", gpu]
        self.proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self.address = address
        self.pool = pool
        self.stats = {}
        self.started_at = time.time()
        self.task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        assert self.proc is not None
        stdout = self.proc.stdout
        if stdout is None:
            return
        async for raw in stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                self.stats = json.loads(line)
            except json.JSONDecodeError:
                continue  # non-JSON line (shouldn't happen — logs go to stderr)

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        if self.proc is not None and self.proc.returncode is None:
            self.proc.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            if self.proc.returncode is None:
                self.proc.kill()
                await self.proc.wait()
        self.proc = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.running(),
            "address": self.address,
            "pool": self.pool,
            "uptime_s": round(time.time() - self.started_at, 1) if self.started_at else 0,
            "stats": self.stats,
        }


_MINER = _Miner()


EARN_TOOL = mcp_types.Tool(
    name="exfer_earn",
    description=(
        "Start earning EXFER for your own address by mining with spare CPU. "
        "EXFER is CPU-mineable, so you can fund yourself from compute alone — no "
        "human, bank, KYC, or pre-funded account, which no card/stablecoin/x402 "
        "rail can offer. Runs a lean fee-free miner in the background against a "
        "public pool; the pool credits your address for the work. Pair with "
        "exfer_earn_status / exfer_earn_stop. Idempotent: a second call while "
        "already mining returns the current status."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "address": {
                "type": "string",
                "description": "Your payout address (64 lowercase hex). Generate one with exfer_generate_address.",
                "pattern": "^[0-9a-f]{64}$",
            },
            "pool": {
                "type": "string",
                "description": f"Stratum pool URL (ssl://host:port). Default: {DEFAULT_POOL}",
            },
            "threads": {
                "type": "integer",
                "description": "CPU threads (0 = auto = cores - 1, or 0 when gpu is set).",
                "minimum": 0,
            },
            "gpu": {
                "type": "string",
                "description": "NVIDIA GPU indices to mine on, e.g. '0' or '0,1'. Needs a CUDA-enabled miner build.",
            },
            "miner_bin": {
                "type": "string",
                "description": "Path to exfer-agent-miner. Default: $EXFER_AGENT_MINER_BIN or PATH.",
            },
        },
        "required": ["address"],
        "additionalProperties": False,
    },
)

EARN_PROBE_TOOL = mcp_types.Tool(
    name="exfer_earn_probe",
    description=(
        "Test whether one or more pools actually accept YOUR shares before you "
        "commit to mining there — pools vary in protocol and some silently reject. "
        "Returns a JSON verdict per pool (reachable / authorized / share_accepted / "
        "hashrate) and a `recommended` pool (the accepting one with the best "
        "hashrate). Use this to choose a pool, then call exfer_earn with it."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "address": {
                "type": "string",
                "description": "Your payout address (64 lowercase hex).",
                "pattern": "^[0-9a-f]{64}$",
            },
            "pools": {
                "type": "array",
                "items": {"type": "string"},
                "description": f"Pool URLs to probe. Default: [{DEFAULT_POOL}].",
            },
            "gpu": {"type": "string", "description": "GPU indices, e.g. '0'. Speeds up probing."},
            "probe_secs": {"type": "integer", "description": "Per-pool timeout seconds (default 35).", "minimum": 5},
            "miner_bin": {"type": "string", "description": "Path to exfer-agent-miner."},
        },
        "required": ["address"],
        "additionalProperties": False,
    },
)

EARN_STATUS_TOOL = mcp_types.Tool(
    name="exfer_earn_status",
    description=(
        "Report the background miner: running, hashrate (H/s), pool-authorized, "
        "jobs received, and shares submitted/accepted/rejected."
    ),
    inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
)

EARN_STOP_TOOL = mcp_types.Tool(
    name="exfer_earn_stop",
    description="Stop the background miner.",
    inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
)


async def earn(
    client: AsyncClient,
    arguments: dict[str, Any],
    config: Config,
) -> list[mcp_types.TextContent]:
    del client, config  # mining is independent of walletd
    if _MINER.running():
        return [json_text({"status": "already_running", **_MINER.snapshot()})]
    bin_path = _resolve_miner_bin(arguments.get("miner_bin"))
    if bin_path is None:
        return [
            text(
                "exfer-agent-miner not found. Build it (cargo build --release -p "
                "exfer-agent-miner) and set EXFER_AGENT_MINER_BIN to the binary, "
                "or put it on PATH."
            )
        ]
    address = arguments["address"]
    pool = arguments.get("pool") or DEFAULT_POOL
    threads = int(arguments.get("threads", 0))
    gpu = str(arguments.get("gpu", ""))
    await _MINER.start(bin_path, address, pool, threads, gpu)
    return [
        json_text({"status": "started", "address": address, "pool": pool, "threads": threads, "gpu": gpu or None})
    ]


async def earn_probe(
    client: AsyncClient,
    arguments: dict[str, Any],
    config: Config,
) -> list[mcp_types.TextContent]:
    del client, config
    bin_path = _resolve_miner_bin(arguments.get("miner_bin"))
    if bin_path is None:
        return [text("exfer-agent-miner not found; set EXFER_AGENT_MINER_BIN or put it on PATH.")]
    address = arguments["address"]
    pools = arguments.get("pools") or [DEFAULT_POOL]
    gpu = str(arguments.get("gpu", ""))
    secs = int(arguments.get("probe_secs", 35))
    verdicts: list[dict[str, Any]] = []
    for pool in pools:
        argv = [bin_path, "--probe", "--pool", pool, "--address", address, "--probe-secs", str(secs)]
        if gpu:
            argv += ["--gpu", gpu]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=secs + 20)
        except (TimeoutError, asyncio.TimeoutError):
            verdicts.append({"pool": pool, "share_accepted": False, "detail": "probe timed out"})
            continue
        verdict: dict[str, Any] = {"pool": pool, "share_accepted": False, "detail": "no verdict"}
        for line in reversed(out.decode("utf-8", "replace").splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    verdict = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
        verdicts.append(verdict)
    verdicts.sort(key=lambda v: (not v.get("share_accepted"), -float(v.get("hashrate") or 0)))
    recommended = next((v["pool"] for v in verdicts if v.get("share_accepted")), None)
    return [json_text({"probed": verdicts, "recommended": recommended})]


async def earn_status(
    client: AsyncClient,
    arguments: dict[str, Any],
    config: Config,
) -> list[mcp_types.TextContent]:
    del client, arguments, config
    return [json_text(_MINER.snapshot())]


async def earn_stop(
    client: AsyncClient,
    arguments: dict[str, Any],
    config: Config,
) -> list[mcp_types.TextContent]:
    del client, arguments, config
    if not _MINER.running():
        return [json_text({"status": "not_running"})]
    await _MINER.stop()
    return [json_text({"status": "stopped"})]
