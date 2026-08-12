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
