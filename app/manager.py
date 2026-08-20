"""ProcessManager: the heart of the hub. Supervises each tool as a subprocess
in its own process group, applies restart policy, and persists running state
so orphans can be reaped after a dashboard crash."""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import time
from pathlib import Path

from . import procutil, registry
from .config import Settings
from .health import check_loopback_bind, wait_healthy
from .logbuf import LogHub
from .models import Manifest, ToolInfo, ToolStatus
from .ports import PortAllocator

log = logging.getLogger(__name__)

RESTART_WINDOW_S = 600  # max_restarts counted within this window
BACKOFF_CAP_S = 60


class ToolSupervisor:
    def __init__(self, tool_id: str, mgr: "ProcessManager"):
        self.tool_id = tool_id
        self.mgr = mgr
        self.status = ToolStatus.STOPPED
        self.proc: asyncio.subprocess.Process | None = None
        self.pgid: int | None = None
        self.started_at: float | None = None
        self.last_exit_code: int | None = None
        self.restart_times: list[float] = []
        self.warnings: list[str] = []
        self.error: str | None = None
        self.cpu_pct: float | None = None
        self.rss_mb: float | None = None
        self.idle_since: float | None = None
        self.stopped_reason: str | None = None
        # (epoch, cpu_pct, rss_mb) sampled every 5s by the monitor: 720 ≈ 1 hour
        self.history: collections.deque[tuple[float, float, float]] = collections.deque(maxlen=720)
        # (epoch, kind, detail) — kind: started|stopped|crashed|gave-up|mem-cap|idle-stop
        self.events: collections.deque[tuple[float, str, str]] = collections.deque(maxlen=50)

    def add_event(self, kind: str, detail: str = "") -> None:
        self.events.append((time.time(), kind, detail))
        self._wait_task: asyncio.Task | None = None
        self._health_task: asyncio.Task | None = None
        self._stopping = False  # user-requested stop in progress

    @property
    def restart_count(self) -> int:
        cutoff = time.monotonic() - RESTART_WINDOW_S
        return len([t for t in self.restart_times if t > cutoff])

    # -- lifecycle ---------------------------------------------------------

    async def start(self, manifest: Manifest, tool_dir: Path, port: int) -> None:
        if self.status in (ToolStatus.RUNNING, ToolStatus.STARTING):
            return
        self._stopping = False
        self.error = None
        self.warnings = []
        self.cpu_pct = None
        self.rss_mb = None
        self.idle_since = None
        self.stopped_reason = None

        chan = self.mgr.logs.channel(self.tool_id, "run")

        # A restart can race the previous holder releasing the port (e.g. container
        # restart while a tool shuts down) — wait briefly before giving up.
        self.status = ToolStatus.STARTING
        for _ in range(10):
            if PortAllocator.is_free(port):
                break
            await asyncio.sleep(1)
        else:
            # Still held after the wait — most likely a stale instance Wharf lost
            # track of (crash, killed dashboard, manual process) rather than a
            # process that's mid-shutdown. Reap it and give the port one more
            # chance before giving up.
            reaped = await procutil.reap_port(port, self.mgr.settings.stop_grace_seconds)
            if reaped is None or not PortAllocator.is_free(port):
                self.status = ToolStatus.ERROR
                self.error = f"port {port} is already in use"
                return
            chan.append(f"--- freed port {port} (was held by untracked pid {reaped})")

        cmd = registry.substitute(manifest.run, port=port, tool_dir=tool_dir)
        env = self._build_env(manifest, tool_dir, port)
        chan.append(f"--- starting: {cmd}")

        try:
            self.proc = await asyncio.create_subprocess_exec(
                "/bin/bash", "-lc", cmd,
                cwd=tool_dir,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as e:
            self.status = ToolStatus.ERROR
            self.error = f"failed to spawn: {e}"
            chan.append(f"--- spawn failed: {e}")
            return

        self.pgid = os.getpgid(self.proc.pid)
        self.started_at = time.time()
        self.status = ToolStatus.STARTING
        self.add_event("started")
        self.mgr.record_running(self.tool_id, self.proc.pid, self.pgid, cmd)

        asyncio.create_task(self._pump_stdout(self.proc, chan))
        self._wait_task = asyncio.create_task(self._supervise(manifest, tool_dir, port))
        self._health_task = asyncio.create_task(self._watch_health(manifest, port))

    async def stop(self) -> None:
        self._stopping = True
        if self._health_task:
            self._health_task.cancel()
        if self.proc is None or self.proc.returncode is not None:
            self.status = ToolStatus.STOPPED
            self.mgr.clear_running(self.tool_id)
            return
        self.status = ToolStatus.STOPPING
        if self.pgid is not None:
            await procutil.kill_pgroup(self.pgid, self.mgr.settings.stop_grace_seconds)
        # _supervise's proc.wait() completes and finalizes state; give it a moment
        if self._wait_task:
            try:
                await asyncio.wait_for(asyncio.shield(self._wait_task), timeout=5)
            except asyncio.TimeoutError:
                pass
        self.status = ToolStatus.STOPPED
        self.mgr.clear_running(self.tool_id)

    # -- internals ---------------------------------------------------------

    def _build_env(self, manifest: Manifest, tool_dir: Path, port: int) -> dict[str, str]:
        env = dict(os.environ)
        env.pop("VIRTUAL_ENV", None)
        env.update({
            "PORT": str(port),
            "HOST": "0.0.0.0",
            "TOOL_NAME": self.tool_id,
            "TOOL_DIR": str(tool_dir),
        })
        venv_bin = tool_dir / ".venv" / "bin"
        if venv_bin.is_dir():
            env["VIRTUAL_ENV"] = str(tool_dir / ".venv")
            env["PATH"] = f"{venv_bin}:{env['PATH']}"
        node_bin = tool_dir / "node_modules" / ".bin"
        if node_bin.is_dir():
            env["PATH"] = f"{node_bin}:{env['PATH']}"
        for k, v in manifest.env.items():
            env[k] = registry.substitute(v, port=port, tool_dir=tool_dir)
        return env

    async def _pump_stdout(self, proc: asyncio.subprocess.Process, chan) -> None:
        assert proc.stdout is not None
        while True:
            try:
                chunk = await proc.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):
                # line longer than the stream limit: read what's buffered and move on
                chunk = await proc.stdout.read(64 * 1024)
            if not chunk:
                break
            chan.append(chunk.decode("utf-8", errors="replace"))

    async def _supervise(self, manifest: Manifest, tool_dir: Path, port: int) -> None:
        assert self.proc is not None
        code = await self.proc.wait()
        self.last_exit_code = code
        self.mgr.clear_running(self.tool_id)
        chan = self.mgr.logs.channel(self.tool_id, "run")
        chan.append(f"--- exited with code {code}")

        if self._stopping:
            self.status = ToolStatus.STOPPED
            self.add_event("stopped")
            return

        should_restart = manifest.restart == "always" or (
            manifest.restart == "on-failure" and code != 0
        )
        if not should_restart:
            self.status = ToolStatus.STOPPED if code == 0 else ToolStatus.CRASHED
            self.add_event("stopped" if code == 0 else "crashed", f"exit {code}")
            return

        self.add_event("crashed", f"exit {code}")
        if self.restart_count >= manifest.max_restarts:
            self.status = ToolStatus.CRASHED
            self.error = f"gave up after {manifest.max_restarts} restarts in {RESTART_WINDOW_S // 60} min"
            chan.append(f"--- {self.error}")
            self.add_event("gave-up", self.error)
            return

        self.status = ToolStatus.CRASHED
        delay = min(2 ** len(self.restart_times[-6:]), BACKOFF_CAP_S)
        self.restart_times.append(time.monotonic())
        chan.append(f"--- restarting in {delay}s (restart {self.restart_count}/{manifest.max_restarts})")
        await asyncio.sleep(delay)
        if not self._stopping:
            await self.start(manifest, tool_dir, port)

    async def _watch_health(self, manifest: Manifest, port: int) -> None:
        if manifest.health.type == "none":
            self.status = ToolStatus.RUNNING
            return
        ok = await wait_healthy(port, manifest.health)
        if self._stopping or self.status not in (ToolStatus.STARTING, ToolStatus.RUNNING, ToolStatus.UNHEALTHY):
            return
        self.status = ToolStatus.RUNNING if ok else ToolStatus.UNHEALTHY
        if ok:
            loopback = check_loopback_bind(port)
            if loopback:
                self.warnings.append(
                    f"listening on {loopback} only — unreachable from your network. "
                    "Make the tool bind 0.0.0.0."
                )
        # slow background probe while running
        while not self._stopping and self.proc and self.proc.returncode is None:
            await asyncio.sleep(30)
            ok = await wait_healthy(port, manifest.health, single_shot=True)
            if self._stopping or self.proc is None or self.proc.returncode is not None:
                return
            if self.status in (ToolStatus.RUNNING, ToolStatus.UNHEALTHY):
                self.status = ToolStatus.RUNNING if ok else ToolStatus.UNHEALTHY


class ProcessManager:
    def __init__(self, settings: Settings, logs: LogHub, ports: PortAllocator):
        self.settings = settings
        self.logs = logs
        self.ports = ports
        self.supervisors: dict[str, ToolSupervisor] = {}
        self.entries: dict[str, registry.RegistryEntry] = {}
        self.install_jobs: dict[str, "object"] = {}  # filled in by installer module
        self.disk_mb: dict[str, float] = {}  # tool_id -> folder size, refreshed by the monitor
        self.git_status: dict[str, dict] = {}  # tool_id -> {ahead, behind, error, checked_at}
        self.running_file = settings.state_dir / "running.json"

    # -- registry ----------------------------------------------------------

    def rescan(self) -> None:
        self.entries = registry.scan(self.settings.tools_dir)

    def entry(self, tool_id: str) -> registry.RegistryEntry | None:
        return self.entries.get(tool_id)

    def supervisor(self, tool_id: str) -> ToolSupervisor:
        if tool_id not in self.supervisors:
            self.supervisors[tool_id] = ToolSupervisor(tool_id, self)
        return self.supervisors[tool_id]

    # -- running.json bookkeeping -------------------------------------------

    def _load_running(self) -> dict[str, dict]:
        try:
            if self.running_file.is_file():
                return json.loads(self.running_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass
        return {}

    def _save_running(self, data: dict[str, dict]) -> None:
        tmp = self.running_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, self.running_file)

    def record_running(self, tool_id: str, pid: int, pgid: int, cmd: str) -> None:
        data = self._load_running()
        data[tool_id] = {"pid": pid, "pgid": pgid, "cmd": cmd, "started_at": time.time()}
        self._save_running(data)

    def clear_running(self, tool_id: str) -> None:
        data = self._load_running()
        if tool_id in data:
            del data[tool_id]
            self._save_running(data)

    # -- boot / shutdown -----------------------------------------------------

    async def boot(self) -> None:
        """Called from FastAPI lifespan startup."""
        stale = self._load_running()
        if stale:
            killed = await procutil.cleanup_orphans(stale)
            if killed:
                log.info("cleaned up orphaned tools from previous run: %s", killed)
            self._save_running({})
        self.rescan()
        for tool_id, entry in self.entries.items():
            if entry.manifest and entry.manifest.autostart:
                asyncio.create_task(self.start_tool(tool_id))
                await asyncio.sleep(0.5)  # stagger spawns

    async def shutdown(self) -> None:
        """Called from FastAPI lifespan shutdown (docker stop → SIGTERM)."""
        running = [s for s in self.supervisors.values()
                   if s.status in (ToolStatus.RUNNING, ToolStatus.STARTING, ToolStatus.UNHEALTHY)]
        if running:
            log.info("stopping %d running tools", len(running))
            await asyncio.gather(*(s.stop() for s in running), return_exceptions=True)

    # -- high-level operations ------------------------------------------------

    def resolve_port(self, tool_id: str) -> int | None:
        entry = self.entry(tool_id)
        if not entry or not entry.manifest:
            return None
        return self.ports.resolve(tool_id, entry.manifest.port)

    def is_installed(self, entry: registry.RegistryEntry) -> bool:
        m = entry.manifest
        if m is None or not m.install:
            return True  # nothing to install
        if m.type == "python" or (entry.path / "requirements.txt").is_file():
            return (entry.path / ".venv").is_dir()
        if m.type == "node" or (entry.path / "package.json").is_file():
            return (entry.path / "node_modules").is_dir()
        return True

    async def start_tool(self, tool_id: str) -> tuple[bool, str]:
        entry = self.entry(tool_id)
        if not entry:
            return False, "unknown tool"
        if entry.manifest is None:
            return False, entry.error or "tool has no manifest"
        sup = self.supervisor(tool_id)
        job = self.install_jobs.get(tool_id)
        if job is not None and getattr(job, "active", False):
            return False, "install in progress"
        if not self.is_installed(entry):
            return False, "tool is not installed yet — run install first"
        port = self.ports.resolve(tool_id, entry.manifest.port)
        await sup.start(entry.manifest, entry.path, port)
        if sup.status == ToolStatus.ERROR:
            return False, sup.error or "failed to start"
        return True, "started"

    async def stop_tool(self, tool_id: str) -> tuple[bool, str]:
        sup = self.supervisors.get(tool_id)
        if not sup:
            return True, "not running"
        await sup.stop()
        return True, "stopped"

    async def restart_tool(self, tool_id: str) -> tuple[bool, str]:
        await self.stop_tool(tool_id)
        return await self.start_tool(tool_id)

    def running_tool_ids(self) -> list[str]:
        return [
            tool_id for tool_id, sup in self.supervisors.items()
            if sup.status in (ToolStatus.RUNNING, ToolStatus.STARTING, ToolStatus.UNHEALTHY)
        ]

    async def apply_port_range(self, lo: int, hi: int) -> None:
        """Change the tools port range: stop whatever's running, drop stale
        auto-assigned ports that now fall outside it, then restart whatever
        was running so it comes back up inside the new range."""
        running = self.running_tool_ids()
        if running:
            await asyncio.gather(*(self.stop_tool(t) for t in running), return_exceptions=True)

        self.settings.tools_port_range = f"{lo}-{hi}"
        pinned_ids = {
            tool_id for tool_id, entry in self.entries.items()
            if entry.manifest and entry.manifest.port
        }
        self.ports.set_range(lo, hi, pinned_ids)

        for tool_id in running:
            await self.start_tool(tool_id)
            await asyncio.sleep(0.5)  # stagger spawns, same as boot()

    # -- status for UI/API -----------------------------------------------------

    def tool_info(self, tool_id: str) -> ToolInfo | None:
        entry = self.entry(tool_id)
        if not entry:
            return None
        sup = self.supervisors.get(tool_id)
        job = self.install_jobs.get(tool_id)

        if entry.manifest is None:
            status = ToolStatus.ERROR if entry.error else ToolStatus.UNCONFIGURED
        elif job is not None and getattr(job, "active", False):
            status = ToolStatus.INSTALLING
        elif sup and sup.status != ToolStatus.STOPPED:
            status = sup.status
        elif not self.is_installed(entry):
            status = ToolStatus.NOT_INSTALLED
        else:
            status = ToolStatus.STOPPED

        port = None
        if entry.manifest:
            try:
                port = self.ports.resolve(tool_id, entry.manifest.port)
            except RuntimeError:
                pass

        warnings = list(sup.warnings) if sup else []
        if entry.manifest and entry.manifest.port and not self.ports.in_range(entry.manifest.port):
            warnings.append(
                f"pinned port {entry.manifest.port} is outside the mapped range "
                f"{self.settings.tools_port_range}"
            )
        if sup and sup.error:
            warnings.append(sup.error)

        alive = bool(sup and sup.proc and sup.proc.returncode is None)
        # last ~5 minutes of samples for the card sparklines
        recent = list(sup.history)[-60:] if alive and sup else []
        git = self.git_status.get(tool_id) or {}
        return ToolInfo(
            id=tool_id,
            manifest=entry.manifest,
            manifest_error=entry.error,
            status=status,
            port=port,
            pid=sup.proc.pid if alive else None,
            started_at=sup.started_at if sup else None,
            restart_count=sup.restart_count if sup else 0,
            last_exit_code=sup.last_exit_code if sup else None,
            warnings=warnings,
            cpu_pct=sup.cpu_pct if alive else None,
            rss_mb=sup.rss_mb if alive else None,
            uptime_s=(time.time() - sup.started_at) if alive and sup.started_at else None,
            stopped_reason=sup.stopped_reason if sup and not alive else None,
            has_git=(entry.path / ".git").is_dir(),
            git_behind=git.get("behind"),
            git_checked_at=git.get("checked_at"),
            disk_mb=self.disk_mb.get(tool_id),
            spark_cpu=[round(c, 1) for _, c, _ in recent],
            spark_rss=[round(r, 1) for _, _, r in recent],
        )

    def all_tools(self) -> list[ToolInfo]:
        return [info for tid in sorted(self.entries) if (info := self.tool_info(tid))]
