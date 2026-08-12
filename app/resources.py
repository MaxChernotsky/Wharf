"""Resource monitoring: per-process-group CPU/RSS, connection counting for idle
detection, and the background loop that enforces idle-stop and memory caps."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .manager import ProcessManager

log = logging.getLogger(__name__)

MONITOR_INTERVAL_S = 5.0


def pgroup_stats() -> dict[int, tuple[float, float]]:
    """One ps sweep: pgid -> (cpu_pct_sum, rss_mb_sum). Works on Linux and macOS."""
    try:
        out = subprocess.run(
            ["ps", "-axo", "pgid=,%cpu=,rss="],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    stats: dict[int, tuple[float, float]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            pgid, cpu, rss_kb = int(parts[0]), float(parts[1]), float(parts[2])
        except ValueError:
            continue
        prev = stats.get(pgid, (0.0, 0.0))
        stats[pgid] = (prev[0] + cpu, prev[1] + rss_kb / 1024)
    return stats


def established_counts(ports: set[int]) -> dict[int, int]:
    """Count ESTABLISHED TCP connections whose local port is in `ports`.

    Uses /proc/net/tcp{,6} on Linux, `netstat -an` elsewhere (macOS dev).
    """
    counts = {p: 0 for p in ports}
    if not ports:
        return counts
    proc_tcp = Path("/proc/net/tcp")
    if proc_tcp.exists():
        for proc_file in (proc_tcp, Path("/proc/net/tcp6")):
            try:
                lines = proc_file.read_text().splitlines()[1:]
            except OSError:
                continue
            for line in lines:
                fields = line.split()
                if len(fields) < 4 or fields[3] != "01":  # 01 = ESTABLISHED
                    continue
                _, _, port_hex = fields[1].partition(":")
                try:
                    port = int(port_hex, 16)
                except ValueError:
                    continue
                if port in counts:
                    counts[port] += 1
        return counts
    # macOS fallback
    try:
        out = subprocess.run(
            ["netstat", "-an", "-p", "tcp"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return counts
    for line in out.splitlines():
        if "ESTABLISHED" not in line:
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        local = parts[3]  # e.g. 192.168.1.5.8100 or *.8100
        port_str = local.rsplit(".", 1)[-1]
        if port_str.isdigit() and int(port_str) in counts:
            counts[int(port_str)] += 1
    return counts


class ResourceMonitor:
    """Background loop: refresh cached stats and enforce idle-stop / memory caps."""

    def __init__(self, mgr: "ProcessManager"):
        self.mgr = mgr
        self._task: asyncio.Task | None = None
        self._tick = 0

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self._collect)
                await self._enforce()
            except Exception:
                log.exception("resource monitor tick failed")
            await asyncio.sleep(MONITOR_INTERVAL_S)

    def _collect(self) -> None:
        # per-tool disk usage is slow-changing: refresh every 12th tick (~1 min)
        if self._tick % 12 == 0:
            self._collect_disk()
        self._tick += 1

        running = {
            tid: sup for tid, sup in self.mgr.supervisors.items()
            if sup.pgid is not None and sup.proc and sup.proc.returncode is None
        }
        if not running:
            return
        stats = pgroup_stats()
        now = time.time()
        ports: set[int] = set()
        for tid, sup in running.items():
            cpu, rss = stats.get(sup.pgid, (0.0, 0.0))
            sup.cpu_pct, sup.rss_mb = cpu, rss
            sup.history.append((now, cpu, rss))
            entry = self.mgr.entry(tid)
            if entry and entry.manifest and entry.manifest.idle_stop_minutes > 0:
                try:
                    ports.add(self.mgr.ports.resolve(tid, entry.manifest.port))
                except RuntimeError:
                    pass
        self._conns = established_counts(ports) if ports else {}

    def _collect_disk(self) -> None:
        dirs = [str(e.path) for e in self.mgr.entries.values()]
        if not dirs:
            return
        try:
            out = subprocess.run(
                ["du", "-sk", *dirs], capture_output=True, text=True, timeout=120,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return
        disk: dict[str, float] = {}
        for line in out.splitlines():
            parts = line.split("\t") if "\t" in line else line.split(None, 1)
            if len(parts) != 2:
                continue
            try:
                disk[Path(parts[1]).name] = float(parts[0]) / 1024
            except ValueError:
                continue
        self.mgr.disk_mb = disk

    async def _enforce(self) -> None:
        now = time.monotonic()
        for tid, sup in list(self.mgr.supervisors.items()):
            if not (sup.proc and sup.proc.returncode is None):
                continue
            entry = self.mgr.entry(tid)
            if not entry or not entry.manifest:
                continue
            m = entry.manifest

            # memory cap
            if m.max_memory_mb > 0 and (sup.rss_mb or 0) > m.max_memory_mb:
                msg = f"memory cap exceeded ({sup.rss_mb:.0f} MB > {m.max_memory_mb} MB) — restarting"
                log.warning("%s: %s", tid, msg)
                self.mgr.logs.channel(tid, "run").append(f"--- {msg}")
                sup.add_event("mem-cap", msg)
                await self.mgr.restart_tool(tid)
                continue

            # idle stop
            if m.idle_stop_minutes > 0:
                try:
                    port = self.mgr.ports.resolve(tid, m.port)
                except RuntimeError:
                    continue
                conns = getattr(self, "_conns", {}).get(port, 0)
                if conns > 0:
                    sup.idle_since = None
                elif sup.idle_since is None:
                    sup.idle_since = now
                elif now - sup.idle_since > m.idle_stop_minutes * 60:
                    msg = f"no connections for {m.idle_stop_minutes} min — stopping (opens again on demand)"
                    log.info("%s: %s", tid, msg)
                    self.mgr.logs.channel(tid, "run").append(f"--- {msg}")
                    await self.mgr.stop_tool(tid)
                    sup.stopped_reason = f"stopped after {m.idle_stop_minutes} min idle"
                    sup.add_event("idle-stop", sup.stopped_reason)
                    sup.idle_since = None
