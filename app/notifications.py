"""Relays tool-reported notifications to Home Assistant's notify service so
they reach a phone. A tool calls `POST /api/tools/<id>/notify` on Wharf's own
API — Wharf never talks to a phone directly, it just forwards the message to
whatever HA notify service is configured (typically a mobile_app service HA
already sets up for you). Keeps a small on-disk history so the Settings page
can show what was sent and whether it worked."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path

import httpx

from .config import Settings
from .models import NotificationRecord

log = logging.getLogger(__name__)

HISTORY_LIMIT = 200
REQUEST_TIMEOUT_S = 10.0


class NotificationHub:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self._transport = transport  # test seam; None uses a real network client
        self._history_path = settings.state_dir / "notifications.json"
        self._history: list[NotificationRecord] = self._load()

    def configured(self) -> bool:
        s = self.settings
        return bool(s.ha_url and s.ha_token and s.ha_notify_devices)

    def recent(self, limit: int = 50, tool_id: str | None = None) -> list[NotificationRecord]:
        items = self._history if tool_id is None else [r for r in self._history if r.tool_id == tool_id]
        return list(reversed(items[-limit:]))

    def _resolve_devices(self, requested: list[str] | None, tool_devices: list[str] | None) -> list[dict]:
        """Priority: an explicit per-call `devices` list, then a tool's own
        `notify_devices` (tool.yml), then the Settings-page default devices,
        then — so a single configured device works with zero extra setup —
        every configured device."""
        ids = requested or tool_devices or self.settings.ha_default_device_ids or [
            d["id"] for d in self.settings.ha_notify_devices
        ]
        by_id = {d["id"]: d for d in self.settings.ha_notify_devices}
        return [by_id[i] for i in ids if i in by_id]

    async def notify(
        self,
        tool_id: str,
        message: str,
        title: str | None = None,
        priority: str | None = None,
        data: dict | None = None,
        devices: list[str] | None = None,
        tool_devices: list[str] | None = None,
    ) -> NotificationRecord:
        targets = self._resolve_devices(devices, tool_devices)
        if not (self.settings.ha_url and self.settings.ha_token):
            ok, error, sent = False, (
                "Home Assistant notifications aren't configured — set a URL and "
                "token on the Settings page"
            ), []
        elif not targets:
            ok, error, sent = False, (
                "no notification device configured or selected — add one on the "
                "Settings page"
            ), []
        else:
            results = await asyncio.gather(
                *(self._send(d["service"], message, title, priority, data) for d in targets)
            )
            ok = all(r_ok for r_ok, _ in results)
            errors = [f"{d['label']}: {r_err}" for d, (r_ok, r_err) in zip(targets, results) if not r_ok]
            error = "; ".join(errors) or None
            sent = [d["label"] for d in targets]
        record = NotificationRecord(
            id=uuid.uuid4().hex,
            tool_id=tool_id,
            title=title,
            message=message,
            priority=priority,
            devices=sent,
            ok=ok,
            error=error,
            created_at=time.time(),
        )
        self._history.append(record)
        del self._history[:-HISTORY_LIMIT]
        self._save()
        return record

    async def _send(
        self, service: str, message: str, title: str | None, priority: str | None, data: dict | None,
    ) -> tuple[bool, str | None]:
        s = self.settings
        payload: dict = {"message": message}
        if title:
            payload["title"] = title
        ha_data = dict(data or {})
        if priority:
            ha_data.setdefault("priority", priority)
        if ha_data:
            payload["data"] = ha_data

        url = f"{s.ha_url.rstrip('/')}/api/services/notify/{service}"
        headers = {"Authorization": f"Bearer {s.ha_token}"}
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S, transport=self._transport) as client:
                resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code >= 400:
                return False, f"Home Assistant returned {resp.status_code}: {resp.text[:300]}"
            return True, None
        except httpx.HTTPError as e:
            log.warning("notification relay to Home Assistant failed: %s", e)
            return False, f"could not reach Home Assistant: {e}"

    # -- persistence -----------------------------------------------------------

    def _load(self) -> list[NotificationRecord]:
        try:
            if self._history_path.is_file():
                raw = json.loads(self._history_path.read_text())
                return [NotificationRecord.model_validate(r) for r in raw]
        except (json.JSONDecodeError, OSError, ValueError):
            pass
        return []

    def _save(self) -> None:
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._history_path.with_suffix(".tmp")
        tmp.write_text(json.dumps([r.model_dump() for r in self._history], indent=2))
        os.replace(tmp, self._history_path)
