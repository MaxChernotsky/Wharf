"""HTML pages and HTMX partials."""

from __future__ import annotations

import time
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from .. import registry
from ..manager import ProcessManager
from ..models import Manifest, ToolStatus
from ..notifications import NotificationHub
from .deps import get_config, get_installer, get_manager, get_notifications

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _uptime(seconds):
    if seconds is None:
        return ""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"


templates.env.filters["uptime"] = _uptime


def _spark_points(values, w: int = 60, h: int = 16) -> str:
    """SVG polyline points for a sparkline, padded 1px top/bottom."""
    if not values or len(values) < 2:
        return ""
    vmin, vmax = min(values), max(values)
    span = (vmax - vmin) or 1.0
    n = len(values)
    pts = []
    for i, v in enumerate(values):
        x = i / (n - 1) * w
        y = (h - 1) - (v - vmin) / span * (h - 2)
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)


templates.env.filters["spark_points"] = _spark_points


def _when(epoch):
    if not epoch:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


templates.env.filters["when"] = _when

ACTIVE_STATUSES = {ToolStatus.RUNNING, ToolStatus.UNHEALTHY, ToolStatus.STARTING, ToolStatus.STOPPING}


def _grid_tools(mgr: ProcessManager):
    """Dashboard order: active tools first, then by name."""
    tools = mgr.all_tools()
    tools.sort(key=lambda t: (t.status not in ACTIVE_STATUSES, t.display_name.lower()))
    return tools


def _totals(tools):
    active = [t for t in tools if t.status in ACTIVE_STATUSES]
    return {
        "running": len(active),
        "total": len(tools),
        "cpu": sum(t.cpu_pct or 0 for t in active),
        "rss": sum(t.rss_mb or 0 for t in active),
        "disk": sum(t.disk_mb or 0 for t in tools),
    }

STATUS_LABELS = {
    ToolStatus.NOT_INSTALLED: ("Not installed", "gray"),
    ToolStatus.INSTALLING: ("Installing…", "blue"),
    ToolStatus.STOPPED: ("Stopped", "gray"),
    ToolStatus.STARTING: ("Starting…", "blue"),
    ToolStatus.RUNNING: ("Running", "green"),
    ToolStatus.UNHEALTHY: ("Unhealthy", "orange"),
    ToolStatus.CRASHED: ("Crashed", "red"),
    ToolStatus.STOPPING: ("Stopping…", "blue"),
    ToolStatus.ERROR: ("Error", "red"),
    ToolStatus.UNCONFIGURED: ("Unconfigured", "gray"),
}


def _ctx(request: Request, mgr: ProcessManager, **extra):
    return {
        "request": request,
        "host": request.url.hostname or "localhost",
        "labels": STATUS_LABELS,
        **extra,
    }


def _sorted_tools(mgr: ProcessManager, sort: str):
    tools = mgr.all_tools()
    if sort == "port":
        tools.sort(key=lambda t: (t.port is None, t.port or 0, t.display_name.lower()))
    else:
        tools.sort(key=lambda t: t.display_name.lower())
    return tools


@router.get("/", response_class=HTMLResponse)
def index(request: Request, mgr: ProcessManager = Depends(get_manager)):
    mgr.rescan()
    tools = _grid_tools(mgr)
    return templates.TemplateResponse(
        request,
        "index.html", _ctx(request, mgr, tools=tools, totals=_totals(tools))
    )


@router.get("/partials/tools", response_class=HTMLResponse)
def partial_tools(request: Request, mgr: ProcessManager = Depends(get_manager)):
    mgr.rescan()
    tools = _grid_tools(mgr)
    return templates.TemplateResponse(
        request,
        "partials/dashboard.html", _ctx(request, mgr, tools=tools, totals=_totals(tools))
    )


@router.post("/partials/tools/{tool_id}/{action}", response_class=HTMLResponse)
async def partial_action(
    tool_id: str,
    action: str,
    request: Request,
    mgr: ProcessManager = Depends(get_manager),
    inst=Depends(get_installer),
):
    mgr.rescan()
    if not mgr.entry(tool_id):
        raise HTTPException(404)
    message = None
    if action == "start":
        ok, message = await mgr.start_tool(tool_id)
    elif action == "stop":
        ok, message = await mgr.stop_tool(tool_id)
    elif action == "restart":
        ok, message = await mgr.restart_tool(tool_id)
    elif action == "install":
        ok, message = await inst.install(tool_id)
    elif action == "rebuild":
        ok, message = await inst.install(tool_id, rebuild=True)
    else:
        raise HTTPException(400, "unknown action")
    info = mgr.tool_info(tool_id)
    return templates.TemplateResponse(
        request,
        "partials/tool_card.html",
        _ctx(request, mgr, tool=info, flash=None if ok else message),
    )


@router.get("/partials/tool-list", response_class=HTMLResponse)
def partial_tool_list(
    request: Request,
    active: str = "",
    sort: str = "name",
    mgr: ProcessManager = Depends(get_manager),
):
    mgr.rescan()
    return templates.TemplateResponse(
        request,
        "partials/tool_list.html",
        _ctx(request, mgr, tools=_sorted_tools(mgr, sort), active=active, sort=sort),
    )


@router.get("/tools/{tool_id}", response_class=HTMLResponse)
def tool_detail(
    request: Request,
    tool_id: str,
    sort: str = "name",
    mgr: ProcessManager = Depends(get_manager),
):
    mgr.rescan()
    entry = mgr.entry(tool_id)
    if not entry:
        raise HTTPException(404)
    info = mgr.tool_info(tool_id)
    manifest_yaml = ""
    mf = entry.path / registry.MANIFEST_FILE
    if mf.is_file():
        manifest_yaml = mf.read_text(encoding="utf-8", errors="replace")
    elif entry.manifest is None:
        suggestion = registry.suggest_manifest(entry.path)
        data = suggestion.model_dump(exclude_none=True, exclude_defaults=True)
        data["run"] = suggestion.run
        manifest_yaml = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    return templates.TemplateResponse(
        request,
        "tool_detail.html",
        _ctx(
            request, mgr,
            tool=info,
            manifest_yaml=manifest_yaml,
            saved=request.query_params.get("saved"),
            tools=_sorted_tools(mgr, sort),
            active=tool_id,
            sort=sort,
        ),
    )


@router.post("/tools/{tool_id}/manifest", response_class=HTMLResponse)
def save_manifest_form(
    request: Request,
    tool_id: str,
    manifest_yaml: str = Form(...),
    mgr: ProcessManager = Depends(get_manager),
):
    mgr.rescan()
    entry = mgr.entry(tool_id)
    if not entry:
        raise HTTPException(404)
    try:
        raw = yaml.safe_load(manifest_yaml)
        if not isinstance(raw, dict):
            raise ValueError("manifest must be a YAML mapping")
        manifest = Manifest.model_validate(raw)
    except (yaml.YAMLError, ValidationError, ValueError) as e:
        info = mgr.tool_info(tool_id)
        return templates.TemplateResponse(
            request,
            "tool_detail.html",
            _ctx(
                request, mgr,
                tool=info,
                manifest_yaml=manifest_yaml,
                manifest_form_error=str(e),
                tools=_sorted_tools(mgr, "name"),
                active=tool_id,
                sort="name",
            ),
            status_code=422,
        )
    registry.save_manifest(entry.path, manifest)
    mgr.rescan()
    return RedirectResponse(f"/tools/{tool_id}?saved=1", status_code=303)


@router.get("/partials/tools/{tool_id}/live", response_class=HTMLResponse)
def partial_tool_live(request: Request, tool_id: str, mgr: ProcessManager = Depends(get_manager)):
    """Status badge + meta + action buttons for the detail page — polled every
    2s so Install/Start finishing (or an Open link appearing) shows up live,
    without a page reload racing the background job."""
    info = mgr.tool_info(tool_id)
    if not info:
        raise HTTPException(404)
    return templates.TemplateResponse(
        request, "partials/tool_live.html", _ctx(request, mgr, tool=info),
    )


@router.get("/partials/tools/{tool_id}/install-status", response_class=HTMLResponse)
def partial_install_status(
    request: Request,
    tool_id: str,
    mgr: ProcessManager = Depends(get_manager),
    inst=Depends(get_installer),
):
    return templates.TemplateResponse(
        request,
        "partials/install_status.html",
        _ctx(request, mgr, tool_id=tool_id, job=inst.job(tool_id)),
    )


@router.get("/launch/{tool_id}", response_class=HTMLResponse)
async def launch(request: Request, tool_id: str, mgr: ProcessManager = Depends(get_manager)):
    """Lazy start: open a stopped tool from a stable URL. Bookmarkable."""
    mgr.rescan()
    entry = mgr.entry(tool_id)
    if not entry:
        raise HTTPException(404)
    info = mgr.tool_info(tool_id)
    port = info.port if info else None
    if port and info.status in (ToolStatus.RUNNING, ToolStatus.UNHEALTHY):
        return RedirectResponse(f"http://{request.url.hostname}:{port}", status_code=302)
    if info.status in (ToolStatus.STOPPED, ToolStatus.CRASHED):
        ok, msg = await mgr.start_tool(tool_id)
        if not ok:
            return RedirectResponse(f"/tools/{tool_id}", status_code=302)
    elif info.status not in (ToolStatus.STARTING,):
        # not installed / unconfigured / error — nothing to launch, show the tool page
        return RedirectResponse(f"/tools/{tool_id}", status_code=302)
    return templates.TemplateResponse(
        request, "launch.html", _ctx(request, mgr, tool=mgr.tool_info(tool_id))
    )


@router.get("/add")
def add_tool_redirect():
    # the add-tool flows live on the settings page now
    return RedirectResponse("/settings", status_code=308)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    mgr: ProcessManager = Depends(get_manager),
    settings=Depends(get_config),
    notifications: NotificationHub = Depends(get_notifications),
):
    mgr.rescan()
    return templates.TemplateResponse(
        request,
        "settings.html",
        _ctx(
            request, mgr,
            settings=settings,
            assignments=mgr.ports.assigned,
            recent_notifications=notifications.recent(10),
        ),
    )
