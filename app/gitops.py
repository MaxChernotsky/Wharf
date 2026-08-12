"""Git integration: repo status for the detail page, pull, clone-from-URL, and a
background scan that checks cloned tools for upstream updates."""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .manager import ProcessManager

log = logging.getLogger(__name__)

GIT_URL_RE = re.compile(r"^(https?://|git@|ssh://)[\w.@:/~-]+$")
UPDATE_CHECK_INTERVAL_S = 30 * 60  # how often the background scan re-fetches each repo


async def _git(path: Path, *args: str, timeout: float = 60) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(path), *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, f"git {' '.join(args)} timed out"
    return proc.returncode or 0, out.decode("utf-8", errors="replace").strip()


def is_repo(path: Path) -> bool:
    return (path / ".git").is_dir()


async def info(path: Path) -> dict:
    """Best-effort repo summary; every field degrades to None on failure."""
    result: dict = {"branch": None, "dirty": None, "last_commit": None, "remote": None, "behind": None}
    code, branch = await _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    if code == 0:
        result["branch"] = branch
    code, status = await _git(path, "status", "--porcelain")
    if code == 0:
        result["dirty"] = bool(status.strip())
    code, commit = await _git(path, "log", "-1", "--format=%h %s (%cr)")
    if code == 0:
        result["last_commit"] = commit
    code, remote = await _git(path, "remote", "get-url", "origin")
    if code == 0:
        result["remote"] = remote
    return result


async def pull(path: Path) -> tuple[bool, str]:
    code, out = await _git(path, "pull", "--ff-only", timeout=300)
    return code == 0, out


async def check_update(path: Path) -> dict:
    """Fetch from origin and compare HEAD against its upstream. Best-effort —
    never raises; a network hiccup or missing upstream just yields an error field
    so the UI can show 'unknown' instead of breaking the scan for every other tool.
    """
    result: dict = {"ahead": None, "behind": None, "error": None, "checked_at": time.time()}
    code, out = await _git(path, "fetch", "--quiet", timeout=60)
    if code != 0:
        result["error"] = out.splitlines()[-1] if out else "git fetch failed"
        return result
    code, upstream = await _git(path, "rev-parse", "--abbrev-ref", "@{u}")
    if code != 0:
        result["error"] = "no upstream tracking branch"
        return result
    code, counts = await _git(path, "rev-list", "--left-right", "--count", "HEAD...@{u}")
    if code != 0 or not counts:
        result["error"] = "could not compare against upstream"
        return result
    parts = counts.split()
    if len(parts) != 2:
        result["error"] = "unexpected git output"
        return result
    result["ahead"], result["behind"] = int(parts[0]), int(parts[1])
    return result


class UpdateMonitor:
    """Background loop: periodically fetch every cloned tool's remote and cache
    how far its local HEAD is behind/ahead of upstream, so the dashboard can flag
    available updates without doing a network call on every page view."""

    def __init__(self, mgr: "ProcessManager"):
        self.mgr = mgr
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                await self.check_all()
            except Exception:
                log.exception("git update scan failed")
            await asyncio.sleep(UPDATE_CHECK_INTERVAL_S)

    async def check_all(self) -> None:
        for tool_id, entry in list(self.mgr.entries.items()):
            if not is_repo(entry.path):
                continue
            self.mgr.git_status[tool_id] = await check_update(entry.path)


async def clone(url: str, staging_dir: Path) -> tuple[Path | None, str]:
    """Clone into a fresh staging dir; returns (repo_path, output)."""
    if not GIT_URL_RE.match(url):
        return None, "that does not look like a git URL"
    dest = staging_dir / f"clone-{uuid.uuid4().hex}"
    proc = await asyncio.create_subprocess_exec(
        "git", "clone", url, str(dest),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
    except asyncio.TimeoutError:
        proc.kill()
        return None, "git clone timed out"
    text = out.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        return None, text
    return dest, text


def default_name_from_url(url: str) -> str:
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return tail.removesuffix(".git")
