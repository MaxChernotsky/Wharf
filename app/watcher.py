"""Dev-mode folder watcher: polls the folder of any tool with `watch: true` in
its tool.yml and restarts the running process a short debounce after the last
change — so editing a tool's source (by hand, or by an AI agent driving it
entirely through the API) is picked up without a manual Restart.

Polling rather than inotify/watchdog on purpose: tool folders on Unraid live
under an appdata share that's frequently edited over SMB or a FUSE mount
(shfs), where inotify events don't reliably cross the mount boundary. A cheap
mtime+size fingerprint sidesteps that and matches the rest of the codebase's
low-dependency style (see resources.py's `du`/`ps` polling).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from . import registry

if TYPE_CHECKING:
    from .manager import ProcessManager

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 2.0

_JUNK_FILE_SUFFIXES = (".pyc", ".pyo", ".log")
_JUNK_FILE_NAMES = {".DS_Store"}

Snapshot = dict[str, tuple[int, int]]  # relpath -> (mtime_ns, size)


def fingerprint(tool_dir: Path) -> Snapshot:
    """relpath -> (mtime_ns, size) for every source file under tool_dir,
    skipping dependency/VCS noise (see registry.NOISE_DIR_NAMES)."""
    out: Snapshot = {}
    for root, dirs, files in os.walk(tool_dir):
        dirs[:] = [d for d in dirs if d not in registry.NOISE_DIR_NAMES]
        for name in files:
            if name in _JUNK_FILE_NAMES or name.endswith(_JUNK_FILE_SUFFIXES):
                continue
            p = Path(root) / name
            try:
                st = p.stat()
            except OSError:
                continue  # deleted mid-walk — next tick will see it as removed
            out[str(p.relative_to(tool_dir))] = (st.st_mtime_ns, st.st_size)
    return out


def describe_diff(old: Snapshot, new: Snapshot) -> str:
    """Short human-readable summary of what changed, or "" if nothing did."""
    added = sorted(new.keys() - old.keys())
    removed = sorted(old.keys() - new.keys())
    changed = sorted(k for k in (old.keys() & new.keys()) if old[k] != new[k])
    if added:
        return f"added {added[0]}" + (f" (+{len(added) - 1} more)" if len(added) > 1 else "")
    if removed:
        return f"removed {removed[0]}" + (f" (+{len(removed) - 1} more)" if len(removed) > 1 else "")
    if changed:
        return f"edited {changed[0]}" + (f" (+{len(changed) - 1} more)" if len(changed) > 1 else "")
    return ""


class _WatchState:
    __slots__ = ("snapshot", "changed_at", "pending")

    def __init__(self) -> None:
        self.snapshot: Snapshot = {}
        self.changed_at: float | None = None  # monotonic time of the last detected diff
        self.pending: str = ""  # description of the change awaiting its debounce


class FolderWatcher:
    """Background loop: fingerprint watch-enabled tools' folders and restart
    the running process once changes go quiet for `watch_debounce_s`."""

    def __init__(self, mgr: "ProcessManager"):
        self.mgr = mgr
        self._task: asyncio.Task | None = None
        self._state: dict[str, _WatchState] = {}

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                await self._tick()
            except Exception:
                log.exception("folder watcher tick failed")
            await asyncio.sleep(POLL_INTERVAL_S)

    async def _tick(self) -> None:
        # rescan so a `watch: true` added via the API/SMB/dashboard a moment
        # ago is picked up without waiting on some other route to trigger it
        self.mgr.rescan()
        now = time.monotonic()

        watched = {
            tid: entry for tid, entry in self.mgr.entries.items()
            if entry.manifest and entry.manifest.watch
        }
        for tid in list(self._state):
            if tid not in watched:
                self._state.pop(tid, None)

        for tid, entry in watched.items():
            sup = self.mgr.supervisors.get(tid)
            running = bool(sup and sup.proc and sup.proc.returncode is None)
            if not running:
                self._state.pop(tid, None)  # re-baseline once it's running again
                continue

            state = self._state.setdefault(tid, _WatchState())
            snapshot = await asyncio.to_thread(fingerprint, entry.path)

            if not state.snapshot:
                state.snapshot = snapshot  # first sight since (re)start: just baseline
                continue

            diff = describe_diff(state.snapshot, snapshot)
            if diff:
                state.snapshot = snapshot
                state.changed_at = now
                state.pending = diff
            elif state.changed_at is not None:
                if now - state.changed_at >= entry.manifest.watch_debounce_s:
                    reason, state.changed_at, state.pending = state.pending, None, ""
                    await self._restart(tid, reason)

    async def _restart(self, tool_id: str, reason: str) -> None:
        log.info("%s: restarting (watch: %s)", tool_id, reason)
        chan = self.mgr.logs.channel(tool_id, "run")
        chan.append(f"--- change detected ({reason}) — restarting")
        sup = self.mgr.supervisors.get(tool_id)
        if sup:
            sup.add_event("watch-restart", reason)
        await self.mgr.restart_tool(tool_id)
