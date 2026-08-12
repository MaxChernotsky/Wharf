"""Background install jobs: uv venv/pip for python tools, npm for node tools."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path

from . import registry
from .manager import ProcessManager
from .models import Manifest

log = logging.getLogger(__name__)


class InstallJob:
    def __init__(self, tool_id: str):
        self.tool_id = tool_id
        self.active = True
        self.ok: bool | None = None
        self.message = "queued"
        self.started_at = time.time()
        self.finished_at: float | None = None


def default_install_cmd(manifest: Manifest, tool_dir: Path) -> str | None:
    if manifest.install:
        return manifest.install
    if manifest.type == "python" or (tool_dir / "requirements.txt").is_file():
        if (tool_dir / "requirements.txt").is_file():
            return "uv pip install -r requirements.txt"
        if (tool_dir / "pyproject.toml").is_file():
            return "uv pip install -e ."
    if manifest.type == "node" or (tool_dir / "package.json").is_file():
        if (tool_dir / "package.json").is_file():
            return "npm ci" if (tool_dir / "package-lock.json").is_file() else "npm install"
    return None


class Installer:
    def __init__(self, mgr: ProcessManager):
        self.mgr = mgr
        self.semaphore = asyncio.Semaphore(mgr.settings.install_concurrency)

    def job(self, tool_id: str) -> InstallJob | None:
        job = self.mgr.install_jobs.get(tool_id)
        return job if isinstance(job, InstallJob) else None

    async def install(self, tool_id: str, *, rebuild: bool = False) -> tuple[bool, str]:
        """Kick off an install in the background. Returns immediately."""
        entry = self.mgr.entry(tool_id)
        if not entry or not entry.manifest:
            return False, "unknown or unconfigured tool"
        existing = self.job(tool_id)
        if existing and existing.active:
            return False, "install already in progress"

        job = InstallJob(tool_id)
        self.mgr.install_jobs[tool_id] = job
        asyncio.create_task(self._run(job, entry.manifest, entry.path, rebuild))
        return True, "install started"

    async def _run(self, job: InstallJob, manifest: Manifest, tool_dir: Path, rebuild: bool) -> None:
        chan = self.mgr.logs.channel(job.tool_id, "install")
        try:
            async with self.semaphore:
                job.message = "preparing"
                # stop the tool if running; a live process holding .venv is trouble
                await self.mgr.stop_tool(job.tool_id)

                if rebuild:
                    for sub in (".venv", "node_modules"):
                        target = tool_dir / sub
                        if target.is_dir():
                            chan.append(f"--- removing {sub}/")
                            await asyncio.to_thread(shutil.rmtree, target, ignore_errors=True)

                steps: list[str] = []
                is_python = manifest.type == "python" or (
                    manifest.type is None
                    and ((tool_dir / "requirements.txt").is_file() or (tool_dir / "pyproject.toml").is_file())
                )
                if is_python and not (tool_dir / ".venv").is_dir():
                    steps.append("uv venv .venv")
                cmd = default_install_cmd(manifest, tool_dir)
                if cmd:
                    steps.append(cmd)
                if not steps:
                    job.ok = True
                    job.message = "nothing to install"
                    return

                env = dict(os.environ)
                env.pop("VIRTUAL_ENV", None)
                if is_python:
                    env["VIRTUAL_ENV"] = str(tool_dir / ".venv")
                    env["PATH"] = f"{tool_dir / '.venv' / 'bin'}:{env['PATH']}"

                for step in steps:
                    step = registry.substitute(step, port=0, tool_dir=tool_dir)
                    job.message = step
                    chan.append(f"--- $ {step}")
                    proc = await asyncio.create_subprocess_exec(
                        "/bin/bash", "-lc", step,
                        cwd=tool_dir,
                        env=env,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                        start_new_session=True,
                    )
                    assert proc.stdout is not None
                    while True:
                        try:
                            line = await proc.stdout.readline()
                        except (ValueError, asyncio.LimitOverrunError):
                            line = await proc.stdout.read(64 * 1024)
                        if not line:
                            break
                        chan.append(line.decode("utf-8", errors="replace"))
                    code = await proc.wait()
                    if code != 0:
                        job.ok = False
                        job.message = f"'{step}' failed with exit code {code}"
                        chan.append(f"--- install FAILED: {job.message}")
                        return

                job.ok = True
                job.message = "installed"
                chan.append("--- install complete")
        except Exception as e:  # never let a job crash silently
            log.exception("install job for %s blew up", job.tool_id)
            job.ok = False
            job.message = f"internal error: {e}"
            chan.append(f"--- install FAILED: {e}")
        finally:
            job.active = False
            job.finished_at = time.time()
