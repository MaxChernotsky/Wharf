"""JSON API for tool lifecycle, manifests, installs, uploads."""

from __future__ import annotations

import logging
import platform
import re
from pathlib import Path

import asyncio
import shutil as _shutil

import yaml
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ValidationError

from .. import config, export, gitops, registry, uploads
from ..installer import Installer
from ..manager import ProcessManager
from ..models import TOOL_ID_RE, Manifest, ToolStatus
from ..notifications import NotificationHub
from .deps import get_config, get_installer, get_manager, get_notifications

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


class FileWrite(BaseModel):
    content: str


class NotifyRequest(BaseModel):
    message: str
    title: str | None = None
    priority: str | None = None
    data: dict | None = None
    device: str | None = None          # target one device id, overriding tool/default config
    devices: list[str] | None = None   # target several device ids (wins over `device`)


class NotifyDevicesRequest(BaseModel):
    devices: list[str] = []


def _require_tool(mgr: ProcessManager, tool_id: str):
    mgr.rescan()
    entry = mgr.entry(tool_id)
    if not entry:
        raise HTTPException(404, f"no tool named {tool_id!r}")
    return entry


@router.get("/tools")
def list_tools(mgr: ProcessManager = Depends(get_manager)):
    mgr.rescan()
    return [t.model_dump() for t in mgr.all_tools()]


@router.get("/tools/{tool_id}")
def get_tool(tool_id: str, mgr: ProcessManager = Depends(get_manager)):
    _require_tool(mgr, tool_id)
    info = mgr.tool_info(tool_id)
    return info.model_dump() if info else {}


@router.post("/tools/{tool_id}/start")
async def start_tool(tool_id: str, mgr: ProcessManager = Depends(get_manager)):
    _require_tool(mgr, tool_id)
    ok, msg = await mgr.start_tool(tool_id)
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": True, "message": msg}


@router.post("/tools/{tool_id}/stop")
async def stop_tool(tool_id: str, mgr: ProcessManager = Depends(get_manager)):
    ok, msg = await mgr.stop_tool(tool_id)
    return {"ok": ok, "message": msg}


@router.post("/tools/{tool_id}/restart")
async def restart_tool(tool_id: str, mgr: ProcessManager = Depends(get_manager)):
    _require_tool(mgr, tool_id)
    ok, msg = await mgr.restart_tool(tool_id)
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": True, "message": msg}


@router.post("/tools/{tool_id}/notify")
async def notify_tool(
    tool_id: str,
    body: NotifyRequest,
    mgr: ProcessManager = Depends(get_manager),
    hub: NotificationHub = Depends(get_notifications),
):
    """A tool's way of telling Wharf it has something to say — Wharf relays
    it to one or more Home Assistant notify-service "devices" configured on
    the Settings page (see app/notifications.py), which is what's actually
    wired up to reach a phone. Wharf itself never talks to a phone directly.
    Which device(s) get it: the request's own `device`/`devices` field, else
    the tool's `notify_devices` in tool.yml, else the Settings-page default."""
    entry = _require_tool(mgr, tool_id)
    requested = body.devices if body.devices else ([body.device] if body.device else None)
    tool_devices = entry.manifest.notify_devices if entry.manifest else None
    record = await hub.notify(
        tool_id, body.message, title=body.title, priority=body.priority, data=body.data,
        devices=requested, tool_devices=tool_devices,
    )
    if not record.ok:
        raise HTTPException(502, record.error or "notification failed")
    return {"ok": True, "id": record.id}


@router.post("/tools/{tool_id}/notify-devices")
def set_tool_notify_devices(
    tool_id: str,
    body: NotifyDevicesRequest,
    mgr: ProcessManager = Depends(get_manager),
):
    """Which Settings-page notification device(s) this tool's own `/notify`
    calls default to — persisted straight into the tool's tool.yml."""
    entry = _require_tool(mgr, tool_id)
    if entry.manifest is None:
        raise HTTPException(409, "save the tool's manifest first")
    manifest = entry.manifest.model_copy(update={"notify_devices": body.devices})
    registry.save_manifest(entry.path, manifest)
    mgr.rescan()
    return {"ok": True}


@router.post("/tools/{tool_id}/install")
async def install_tool(
    tool_id: str,
    rebuild: bool = False,
    mgr: ProcessManager = Depends(get_manager),
    inst: Installer = Depends(get_installer),
):
    _require_tool(mgr, tool_id)
    ok, msg = await inst.install(tool_id, rebuild=rebuild)
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": True, "message": msg}


@router.get("/tools/{tool_id}/install/status")
def install_status(tool_id: str, inst: Installer = Depends(get_installer)):
    job = inst.job(tool_id)
    if not job:
        return {"state": "none"}
    return {
        "state": "running" if job.active else ("ok" if job.ok else "failed"),
        "message": job.message,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }


@router.put("/tools/{tool_id}/manifest")
def save_manifest(
    tool_id: str,
    body: dict,
    mgr: ProcessManager = Depends(get_manager),
):
    entry = _require_tool(mgr, tool_id)
    try:
        manifest = Manifest.model_validate(body)
    except ValidationError as e:
        raise HTTPException(422, str(e))
    registry.save_manifest(entry.path, manifest)
    mgr.rescan()
    return {"ok": True}


@router.post("/tools/{tool_id}/manifest/suggest")
def suggest_manifest(tool_id: str, mgr: ProcessManager = Depends(get_manager)):
    entry = _require_tool(mgr, tool_id)
    return registry.suggest_manifest(entry.path).model_dump(exclude_none=True)


@router.post("/tools/{tool_id}/watch")
def set_watch(tool_id: str, enabled: bool = True, mgr: ProcessManager = Depends(get_manager)):
    """Toggle dev-mode auto-restart-on-change without round-tripping the whole
    manifest — see `watch` in tool.yml and app/watcher.py."""
    entry = _require_tool(mgr, tool_id)
    if not entry.manifest:
        raise HTTPException(409, "tool has no manifest yet — save one first")
    manifest = entry.manifest.model_copy(update={"watch": enabled})
    registry.save_manifest(entry.path, manifest)
    mgr.rescan()
    return {"ok": True, "watch": enabled}


@router.delete("/tools/{tool_id}")
async def delete_tool(
    tool_id: str,
    delete_files: bool = False,
    mgr: ProcessManager = Depends(get_manager),
):
    entry = _require_tool(mgr, tool_id)
    await mgr.stop_tool(tool_id)
    mgr.ports.release(tool_id)
    mgr.supervisors.pop(tool_id, None)
    mgr.install_jobs.pop(tool_id, None)
    mgr.logs.drop(tool_id)
    if delete_files:
        # linked tools live behind a symlink — remove_tool_dir only ever
        # unlinks that, never the real folder it points to
        uploads.remove_tool_dir(entry.path)
    mgr.rescan()
    return {"ok": True, "deleted_files": delete_files}


@router.post("/tools")
def create_tool(
    body: dict,
    mgr: ProcessManager = Depends(get_manager),
    settings=Depends(get_config),
):
    """Scaffold a brand-new tool from inline file contents — the API-only path
    for an AI agent to build a tool with no local filesystem access to wherever
    Wharf's tools_dir actually lives. `files` is {relative_path: text_content};
    `manifest`, if given, is validated and written as tool.yml immediately —
    otherwise a suggested one is returned for review (same shape as
    /manifest/suggest) and can be saved with PUT /tools/{id}/manifest."""
    tool_id = body.get("tool_id") or ""
    files = body.get("files") or {}
    if not isinstance(files, dict) or not files:
        raise HTTPException(400, "files must be a non-empty {path: content} object")

    manifest: Manifest | None = None
    if body.get("manifest") is not None:
        try:
            manifest = Manifest.model_validate(body["manifest"])
        except ValidationError as e:
            raise HTTPException(422, str(e))

    try:
        tool_dir = uploads.create_tool_dir(tool_id, files, settings)
        if manifest:
            registry.save_manifest(tool_dir, manifest)
    except uploads.UploadError as e:
        raise HTTPException(e.status_code, str(e))

    mgr.rescan()
    entry = mgr.entry(tool_id)
    return {
        "ok": True,
        "tool_id": tool_id,
        "has_manifest": bool(entry and entry.manifest),
        "manifest_error": entry.error if entry else None,
        "suggested_manifest": (
            None if manifest else registry.suggest_manifest(tool_dir).model_dump(exclude_none=True)
        ),
    }


@router.get("/tools/{tool_id}/files")
def list_files(tool_id: str, mgr: ProcessManager = Depends(get_manager)):
    entry = _require_tool(mgr, tool_id)
    return {"files": registry.list_files(entry.path)}


@router.get("/tools/{tool_id}/files/{path:path}")
def read_file(tool_id: str, path: str, mgr: ProcessManager = Depends(get_manager)):
    entry = _require_tool(mgr, tool_id)
    try:
        content = uploads.read_tool_file(entry.path, path)
    except uploads.UploadError as e:
        raise HTTPException(e.status_code, str(e))
    return {"path": path, "content": content}


@router.put("/tools/{tool_id}/files/{path:path}")
def write_file(
    tool_id: str,
    path: str,
    body: FileWrite,
    mgr: ProcessManager = Depends(get_manager),
    settings=Depends(get_config),
):
    """Create or overwrite one file in a tool's folder. Works even if the tool
    doesn't exist yet — the first write creates its folder, so an AI agent can
    build a tool one file at a time instead of a single bulk POST /tools call.
    If the tool is running with `watch: true`, it restarts on its own shortly
    after — no explicit restart call needed."""
    if not TOOL_ID_RE.match(tool_id):
        raise HTTPException(400, f"invalid tool id: {tool_id!r}")
    tool_dir = settings.tools_dir / tool_id
    tool_dir.mkdir(parents=True, exist_ok=True)
    try:
        uploads.write_tool_file(tool_dir, path, body.content)
    except uploads.UploadError as e:
        raise HTTPException(e.status_code, str(e))
    mgr.rescan()
    return {"ok": True, "path": path}


@router.delete("/tools/{tool_id}/files/{path:path}")
def delete_file(tool_id: str, path: str, mgr: ProcessManager = Depends(get_manager)):
    entry = _require_tool(mgr, tool_id)
    try:
        uploads.delete_tool_file(entry.path, path)
    except uploads.UploadError as e:
        raise HTTPException(e.status_code, str(e))
    mgr.rescan()
    return {"ok": True}


@router.post("/tools/upload")
async def upload_tool(
    file: UploadFile = File(...),
    name: str = Form(""),
    replace: bool = Form(False),
    mgr: ProcessManager = Depends(get_manager),
    settings=Depends(get_config),
):
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(400, "only .zip uploads are supported")

    tmp_zip = settings.staging_dir / f"upload-{file.filename}"
    size = 0
    with open(tmp_zip, "wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > settings.upload_max_zip_bytes:
                out.close()
                tmp_zip.unlink(missing_ok=True)
                raise HTTPException(413, "zip file too large")
            out.write(chunk)

    try:
        src = uploads.extract_zip(tmp_zip, settings)
        # explicit name wins; else the unwrapped folder's name; else the zip filename
        # (src directly under staging_dir means no top-level folder was unwrapped,
        # so its name is a random staging id — fall back to the filename)
        if name:
            candidate = name
        elif src.parent.resolve() != settings.staging_dir.resolve():
            candidate = src.name
        else:
            candidate = file.filename or "tool"
        tool_id = uploads.sanitize_tool_id(candidate)
        dest = uploads.install_tool_dir(src, tool_id, settings, replace=replace)
        uploads.cleanup_staging(src, settings)
    except uploads.UploadError as e:
        raise HTTPException(e.status_code, str(e))
    finally:
        tmp_zip.unlink(missing_ok=True)

    mgr.rescan()
    has_manifest = (dest / registry.MANIFEST_FILE).is_file()
    return {"ok": True, "tool_id": tool_id, "has_manifest": has_manifest}


@router.post("/tools/{tool_id}/update")
async def stage_tool_update(
    tool_id: str,
    file: UploadFile = File(...),
    mgr: ProcessManager = Depends(get_manager),
    settings=Depends(get_config),
):
    """Stage a new zip as a pending update for an already-installed tool.
    Doesn't touch the live tool dir — see POST .../update/apply for that."""
    entry = _require_tool(mgr, tool_id)
    if entry.path.is_symlink():
        raise HTTPException(400, "tool is dev-linked — edit its source directory directly instead")
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(400, "only .zip uploads are supported")

    tmp_zip = settings.staging_dir / f"update-{tool_id}-{file.filename}"
    size = 0
    with open(tmp_zip, "wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > settings.upload_max_zip_bytes:
                out.close()
                tmp_zip.unlink(missing_ok=True)
                raise HTTPException(413, "zip file too large")
            out.write(chunk)

    try:
        uploads.stage_update(tmp_zip, tool_id, settings)
    except uploads.UploadError as e:
        raise HTTPException(e.status_code, str(e))
    finally:
        tmp_zip.unlink(missing_ok=True)

    return {"ok": True, "staged": True}


@router.post("/tools/{tool_id}/update/apply")
async def apply_tool_update(
    tool_id: str,
    mgr: ProcessManager = Depends(get_manager),
    inst: Installer = Depends(get_installer),
    settings=Depends(get_config),
):
    """Swap a staged update into place (data/ preserved), reinstall, and
    restart the tool if it was running — same shape as git/pull."""
    _require_tool(mgr, tool_id)
    info = mgr.tool_info(tool_id)
    was_running = bool(info and info.status in (ToolStatus.RUNNING, ToolStatus.UNHEALTHY, ToolStatus.STARTING))
    if was_running:
        await mgr.stop_tool(tool_id)  # a live process holding the tool dir is trouble mid-swap

    chan = mgr.logs.channel(tool_id, "install")
    chan.append("--- applying staged zip update")
    try:
        uploads.apply_pending_update(tool_id, settings)
    except uploads.UploadError as e:
        chan.append(f"--- update FAILED: {e}")
        raise HTTPException(e.status_code, str(e))

    mgr.rescan()
    await inst.install(tool_id)  # background; new source often means new deps

    async def restart_when_installed():
        for _ in range(600):
            job = inst.job(tool_id)
            if job and not job.active:
                if job.ok:
                    await mgr.start_tool(tool_id)
                return
            await asyncio.sleep(1)
    if was_running:
        asyncio.create_task(restart_when_installed())
    return {"ok": True, "reinstalling": True}


@router.delete("/tools/{tool_id}/update")
def discard_tool_update(
    tool_id: str,
    mgr: ProcessManager = Depends(get_manager),
    settings=Depends(get_config),
):
    _require_tool(mgr, tool_id)
    uploads.discard_pending_update(tool_id, settings)
    return {"ok": True}


@router.get("/tools/{tool_id}/stats")
def tool_stats(tool_id: str, mgr: ProcessManager = Depends(get_manager)):
    """History + event log for the detail-page charts."""
    _require_tool(mgr, tool_id)
    sup = mgr.supervisors.get(tool_id)
    if not sup:
        return {"history": [], "events": []}
    return {
        "history": [[round(t), round(c, 1), round(r, 1)] for t, c, r in sup.history],
        "events": [[round(t), kind, detail] for t, kind, detail in sup.events],
    }


@router.get("/tools/{tool_id}/git")
async def git_info(tool_id: str, mgr: ProcessManager = Depends(get_manager)):
    entry = _require_tool(mgr, tool_id)
    if not gitops.is_repo(entry.path):
        raise HTTPException(404, "not a git repository")
    info = await gitops.info(entry.path)
    # cached by the background scan (app.gitops.UpdateMonitor) — cheap to include,
    # no network call on every page view
    info.update(mgr.git_status.get(tool_id, {}))
    return info


@router.post("/tools/{tool_id}/git/check")
async def git_check_update(tool_id: str, mgr: ProcessManager = Depends(get_manager)):
    """Fetch now and refresh the cached ahead/behind counts (same check the
    background scan runs every 30 minutes, triggered on demand)."""
    entry = _require_tool(mgr, tool_id)
    if not gitops.is_repo(entry.path):
        raise HTTPException(404, "not a git repository")
    result = await gitops.check_update(entry.path)
    mgr.git_status[tool_id] = result
    return result


@router.post("/tools/{tool_id}/git/pull")
async def git_pull(
    tool_id: str,
    mgr: ProcessManager = Depends(get_manager),
    inst: Installer = Depends(get_installer),
):
    """Pull, reinstall, and restart the tool if it was running."""
    entry = _require_tool(mgr, tool_id)
    if not gitops.is_repo(entry.path):
        raise HTTPException(404, "not a git repository")
    info = mgr.tool_info(tool_id)
    was_running = info and info.status in (ToolStatus.RUNNING, ToolStatus.UNHEALTHY, ToolStatus.STARTING)

    chan = mgr.logs.channel(tool_id, "install")
    chan.append("--- $ git pull --ff-only")
    ok, out = await gitops.pull(entry.path)
    for line in out.splitlines():
        chan.append(line)
    if not ok:
        raise HTTPException(409, f"git pull failed: {out.splitlines()[-1] if out else 'unknown error'}")

    mgr.rescan()
    up_to_date = "Already up to date" in out
    if not up_to_date:
        await inst.install(tool_id)  # background; stops the tool first
        if was_running:
            # restart once the install completes
            async def restart_when_installed():
                for _ in range(600):
                    job = inst.job(tool_id)
                    if job and not job.active:
                        if job.ok:
                            await mgr.start_tool(tool_id)
                        return
                    await asyncio.sleep(1)
            asyncio.create_task(restart_when_installed())
    return {"ok": True, "output": out, "up_to_date": up_to_date, "reinstalling": not up_to_date}


@router.post("/tools/clone")
async def clone_tool(
    url: str = Form(...),
    name: str = Form(""),
    mgr: ProcessManager = Depends(get_manager),
    settings=Depends(get_config),
):
    repo, out = await gitops.clone(url.strip(), settings.staging_dir)
    if repo is None:
        raise HTTPException(400, out)
    try:
        tool_id = uploads.sanitize_tool_id(name or gitops.default_name_from_url(url))
        dest = uploads.install_tool_dir(repo, tool_id, settings)
    except uploads.UploadError as e:
        _shutil.rmtree(repo, ignore_errors=True)
        raise HTTPException(e.status_code, str(e))
    mgr.rescan()
    has_manifest = (dest / registry.MANIFEST_FILE).is_file()
    return {"ok": True, "tool_id": tool_id, "has_manifest": has_manifest}


@router.post("/tools/browse")
async def browse_folder():
    """Pop a native folder picker on the machine running Wharf and return the
    chosen path — a convenience for the "link a local folder" form so you
    don't have to type an absolute path by hand. Only works when Wharf runs
    locally on macOS with a GUI session (osascript drives Finder's picker);
    on a headless host (e.g. the Docker/Unraid deployment) it fails clearly
    rather than silently doing nothing."""
    if platform.system() != "Darwin":
        raise HTTPException(501, "the folder picker only works when Wharf runs locally on macOS")
    script = 'POSIX path of (choose folder with prompt "Select a tool folder for Wharf")'
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=300)
    except FileNotFoundError:
        raise HTTPException(501, "osascript is not available — can't open a native folder picker here")
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(408, "folder picker timed out")

    if proc.returncode != 0:
        text = err.decode("utf-8", errors="replace").strip()
        if "-128" in text:  # user clicked Cancel
            return {"path": None}
        raise HTTPException(500, text or "folder picker failed")
    return {"path": out.decode("utf-8", errors="replace").strip()}


@router.post("/menubar/launch")
async def launch_menubar():
    """Start the macOS menubar companion app (macos-menubar/wharf_menubar.py)
    as a detached background process. Same constraint as /tools/browse: only
    works when Wharf itself runs locally on macOS with a GUI session, since
    the menu bar it opens belongs to the machine actually running Wharf, not
    whatever machine happens to have this page open."""
    if platform.system() != "Darwin":
        raise HTTPException(501, "the menubar app only runs when Wharf is on macOS locally")

    repo_root = Path(__file__).resolve().parent.parent.parent
    script = repo_root / "macos-menubar" / "wharf_menubar.py"
    if not script.is_file():
        raise HTTPException(404, "macos-menubar/wharf_menubar.py not found next to this checkout")

    try:
        pgrep = await asyncio.create_subprocess_exec(
            "pgrep", "-f", str(script),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        if await pgrep.wait() == 0:
            return {"ok": True, "message": "already running"}
    except FileNotFoundError:
        pass  # no pgrep on this system — fall through and just (re)launch

    venv_python = repo_root / "macos-menubar" / ".venv" / "bin" / "python"
    python = str(venv_python) if venv_python.is_file() else "python3"

    try:
        proc = await asyncio.create_subprocess_exec(
            python, str(script),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,  # survive the dashboard process reloading/restarting
        )
    except FileNotFoundError:
        raise HTTPException(501, f"{python} not found — set up macos-menubar/.venv first (see its README)")

    await asyncio.sleep(0.6)  # long enough for an immediate import/crash to surface
    if proc.returncode is not None and proc.returncode != 0:
        out = (await proc.stdout.read()).decode("utf-8", errors="replace").strip()
        raise HTTPException(500, out[-500:] if out else "menubar app exited immediately")
    return {"ok": True, "message": "menubar app launched"}


@router.post("/tools/link")
def link_tool(
    path: str = Form(...),
    name: str = Form(""),
    mgr: ProcessManager = Depends(get_manager),
    settings=Depends(get_config),
):
    """Link to an existing folder instead of copying it in — for active
    development, so the tool keeps living in your own checkout and edits
    there are what actually runs. `path` must already be visible to the
    Wharf process itself (a local path in dev, or a path bind-mounted into
    the container in Docker) — nothing is copied over the network. Defaults
    `watch` on for a fresh manifest, since the whole point of linking is
    picking up edits automatically."""
    src = Path(path)
    try:
        tool_id = uploads.sanitize_tool_id(name or src.expanduser().name)
        dest = uploads.link_tool_dir(tool_id, src, settings)
    except uploads.UploadError as e:
        raise HTTPException(e.status_code, str(e))

    mgr.rescan()
    entry = mgr.entry(tool_id)
    has_manifest = bool(entry and entry.manifest)
    return {
        "ok": True,
        "tool_id": tool_id,
        "linked_path": str(dest.resolve()),
        "has_manifest": has_manifest,
        "manifest_error": entry.error if entry else None,
        "suggested_manifest": (
            None if has_manifest else registry.suggest_manifest(dest).model_dump(exclude_none=True)
        ),
    }


@router.get("/tools/{tool_id}/export")
def export_tool(
    tool_id: str,
    background: BackgroundTasks,
    include_git: bool = False,
    mgr: ProcessManager = Depends(get_manager),
    settings=Depends(get_config),
):
    entry = _require_tool(mgr, tool_id)
    zip_path = export.build_export_zip(entry.path, settings.staging_dir, include_git=include_git)
    background.add_task(_shutil.rmtree, zip_path.parent, True)
    return FileResponse(zip_path, filename=zip_path.name, media_type="application/zip")


@router.post("/rescan")
def rescan(mgr: ProcessManager = Depends(get_manager)):
    mgr.rescan()
    return {"ok": True, "tools": sorted(mgr.entries)}


@router.get("/settings")
def settings_info(mgr: ProcessManager = Depends(get_manager), settings=Depends(get_config)):
    return {
        "tools_dir": str(settings.tools_dir),
        "port_range": settings.tools_port_range,
        "assignments": mgr.ports.assigned,
    }


def _parse_port_range(raw: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d{1,5})\s*-\s*(\d{1,5})\s*", raw)
    if not m:
        raise ValueError("must look like 8100-8199")
    lo, hi = int(m.group(1)), int(m.group(2))
    if not (1 <= lo <= hi <= 65535):
        raise ValueError("ports must be between 1 and 65535, with start ≤ end")
    return lo, hi


@router.post("/settings/port-range")
async def update_port_range(
    port_range: str = Form(...),
    mgr: ProcessManager = Depends(get_manager),
    settings=Depends(get_config),
):
    """Change the range hosted tools are assigned ports from. Stops and
    restarts whatever's currently running so it comes back up inside the
    new range, and persists the change so it survives a container restart.
    Note: the actual TCP ports only become reachable if the Docker/Unraid
    port mapping is also updated to match — this only changes what Wharf
    assigns inside the container."""
    try:
        lo, hi = _parse_port_range(port_range)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if lo <= settings.dashboard_port <= hi:
        raise HTTPException(400, f"range can't include the dashboard port ({settings.dashboard_port})")
    normalized = f"{lo}-{hi}"
    config.save_overrides(settings.state_dir, tools_port_range=normalized)
    await mgr.apply_port_range(lo, hi)
    return {"ok": True, "port_range": normalized}


def manifest_to_yaml(manifest: Manifest) -> str:
    data = manifest.model_dump(exclude_none=True, exclude_defaults=True)
    data["run"] = manifest.run
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
