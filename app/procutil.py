"""Process-group helpers: graceful kill with escalation, orphan cleanup after crashes."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def pgid_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


async def kill_pgroup(pgid: int, grace_seconds: float) -> None:
    """SIGTERM the group, wait up to grace_seconds, then SIGKILL survivors."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = asyncio.get_event_loop().time() + grace_seconds
    while asyncio.get_event_loop().time() < deadline:
        if not pgid_alive(pgid):
            return
        await asyncio.sleep(0.2)
    try:
        os.killpg(pgid, signal.SIGKILL)
        log.warning("pgid %d did not exit within %.0fs; sent SIGKILL", pgid, grace_seconds)
    except ProcessLookupError:
        pass


def proc_cmdline(pid: int) -> str:
    """Best-effort cmdline read; empty string if the process is gone or unreadable.

    Uses /proc on Linux and falls back to `ps` where /proc doesn't exist (macOS dev).
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
    except OSError:
        pass
    try:
        out = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _listening_inode(port: int) -> str | None:
    """Linux only: the socket inode of whatever is LISTENing on `port`, by
    scanning /proc/net/tcp(6). None if unreadable (e.g. macOS dev) or not found."""
    port_hex = f"{port:04X}"
    for proc_file in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        try:
            lines = proc_file.read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A":  # 0A = LISTEN
                continue
            _, _, p_hex = fields[1].partition(":")
            if p_hex.upper() == port_hex:
                return fields[9]
    return None


def _pid_for_inode(inode: str) -> int | None:
    """Linux only: scan /proc/*/fd for the pid holding an open socket inode."""
    target = f"socket:[{inode}]"
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return None
    for pid in pids:
        try:
            for fd in Path(f"/proc/{pid}/fd").iterdir():
                try:
                    if os.readlink(fd) == target:
                        return int(pid)
                except OSError:
                    continue
        except OSError:
            continue
    return None


def find_port_pid(port: int) -> int | None:
    """Best-effort pid of whatever is currently LISTENing on `port`, or None.

    Uses /proc on Linux (the container's runtime) and falls back to `lsof`
    where /proc doesn't exist (macOS dev).
    """
    inode = _listening_inode(port)
    if inode is not None:
        pid = _pid_for_inode(inode)
        if pid is not None:
            return pid
    try:
        out = subprocess.run(
            ["lsof", f"-tiTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5,
        )
        first = out.stdout.split()
        if first:
            return int(first[0])
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return None


async def reap_port(port: int, grace_seconds: float) -> int | None:
    """Find whatever is squatting on `port` (a stale instance Wharf lost
    track of — see manager.ToolSupervisor.start) and kill its process group
    with the same SIGTERM-then-SIGKILL escalation as a normal stop.
    Returns the killed pid, or None if the port wasn't held by anything we
    could find.
    """
    pid = find_port_pid(port)
    if pid is None:
        return None
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return None
    log.warning(
        "port %d is held by untracked pid %d (pgid %d); killing it to free the port",
        port, pid, pgid,
    )
    await kill_pgroup(pgid, grace_seconds)
    return pid


async def cleanup_orphans(records: dict[str, dict], grace_seconds: float = 5.0) -> list[str]:
    """Kill process groups recorded in running.json by a previous dashboard instance.

    Only kills a group when the recorded pid is alive and its cmdline plausibly
    matches what we recorded, to avoid shooting an unrelated recycled pid.
    Returns the list of tool ids that had survivors killed.
    """
    killed: list[str] = []
    for tool_id, rec in records.items():
        pid = rec.get("pid")
        pgid = rec.get("pgid")
        if not pid or not pgid:
            continue
        cmdline = proc_cmdline(int(pid))
        if not cmdline:
            continue  # pid gone — nothing to do
        recorded = str(rec.get("cmd", ""))
        # bash -lc <cmd> shows the cmd in the cmdline; require a loose match
        if recorded and recorded[:40] not in cmdline:
            log.warning(
                "running.json pid %s for %s has unrelated cmdline %r; not killing",
                pid, tool_id, cmdline[:80],
            )
            continue
        log.info("killing orphaned process group %s for tool %s", pgid, tool_id)
        await kill_pgroup(int(pgid), grace_seconds)
        killed.append(tool_id)
    return killed
