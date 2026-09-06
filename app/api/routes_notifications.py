"""Home Assistant notify relay: settings, history, and a test send. The
per-tool `POST /api/tools/<id>/notify` endpoint that tools actually call
lives in routes_tools.py, alongside the rest of a tool's lifecycle API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form

from .. import config
from ..notifications import NotificationHub
from .deps import get_config, get_notifications

router = APIRouter(prefix="/api/notifications")


@router.get("")
def list_notifications(
    limit: int = 50,
    tool_id: str | None = None,
    hub: NotificationHub = Depends(get_notifications),
):
    return [r.model_dump() for r in hub.recent(limit=limit, tool_id=tool_id)]


@router.get("/settings")
def notification_settings(settings=Depends(get_config)):
    return {
        "ha_url": settings.ha_url,
        "ha_notify_service": settings.ha_notify_service,
        "has_token": bool(settings.ha_token),
        "configured": bool(settings.ha_url and settings.ha_token and settings.ha_notify_service),
    }


@router.post("/settings")
async def update_notification_settings(
    ha_url: str = Form(""),
    ha_notify_service: str = Form(""),
    ha_token: str = Form(""),
    clear_token: bool = Form(False),
    settings=Depends(get_config),
):
    """Blank `ha_token` leaves whatever's already saved untouched, so editing
    the URL or service name doesn't force retyping the token — pass
    `clear_token=true` to remove it explicitly."""
    token = "" if clear_token else (ha_token.strip() or settings.ha_token)
    config.save_overrides(
        settings.state_dir,
        ha_url=ha_url.strip(),
        ha_notify_service=ha_notify_service.strip(),
        ha_token=token,
    )
    settings.ha_url = ha_url.strip()
    settings.ha_notify_service = ha_notify_service.strip()
    settings.ha_token = token
    return {"ok": True}


@router.post("/test")
async def send_test_notification(hub: NotificationHub = Depends(get_notifications)):
    record = await hub.notify(
        "wharf", "This is a test notification from Wharf.", title="Wharf test notification",
    )
    return {"ok": record.ok, "error": record.error}
