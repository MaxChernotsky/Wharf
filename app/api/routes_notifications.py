"""Home Assistant notify relay: settings, history, and a test send. The
per-tool `POST /api/tools/<id>/notify` endpoint that tools actually call
lives in routes_tools.py, alongside the rest of a tool's lifecycle API."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .. import config
from ..notifications import NotificationHub
from .deps import get_config, get_notifications

router = APIRouter(prefix="/api/notifications")


class NotifyDeviceIn(BaseModel):
    id: str
    label: str
    service: str


class NotificationSettingsIn(BaseModel):
    ha_url: str = ""
    ha_token: str = ""
    clear_token: bool = False
    devices: list[NotifyDeviceIn] = Field(default_factory=list)
    default_device_ids: list[str] = Field(default_factory=list)


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
        "devices": settings.ha_notify_devices,
        "default_device_ids": settings.ha_default_device_ids,
        "has_token": bool(settings.ha_token),
        "configured": bool(settings.ha_url and settings.ha_token and settings.ha_notify_devices),
    }


@router.post("/settings")
async def update_notification_settings(body: NotificationSettingsIn, settings=Depends(get_config)):
    """Blank `ha_token` leaves whatever's already saved untouched, so editing
    the URL or a device doesn't force retyping the token — pass
    `clear_token=true` to remove it explicitly."""
    token = "" if body.clear_token else (body.ha_token.strip() or settings.ha_token)
    devices = [d.model_dump() for d in body.devices]
    known_ids = {d["id"] for d in devices}
    default_ids = [i for i in body.default_device_ids if i in known_ids]
    config.save_overrides(
        settings.state_dir,
        ha_url=body.ha_url.strip(),
        ha_token=token,
        ha_notify_devices=devices,
        ha_default_device_ids=default_ids,
    )
    settings.ha_url = body.ha_url.strip()
    settings.ha_token = token
    settings.ha_notify_devices = devices
    settings.ha_default_device_ids = default_ids
    return {"ok": True}


@router.post("/test")
async def send_test_notification(device_id: str | None = None, hub: NotificationHub = Depends(get_notifications)):
    record = await hub.notify(
        "wharf", "This is a test notification from Wharf.", title="Wharf test notification",
        devices=[device_id] if device_id else None,
    )
    return {"ok": record.ok, "error": record.error}
