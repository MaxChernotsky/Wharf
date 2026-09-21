"""Settings loaded from environment variables (set by Dockerfile / Unraid template),
with a small set of fields user-overridable at runtime from the settings page."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings

# fields the settings UI is allowed to persist over the environment defaults
OVERRIDABLE_FIELDS = {
    "tools_port_range", "ha_url", "ha_token", "ha_notify_devices", "ha_default_device_ids",
    "auth_password_hash", "auth_api_token",
}


class Settings(BaseSettings):
    data_dir: Path = Path("/data")
    tools_dir: Path = Path("/data/tools")
    dashboard_port: int = 8080
    tools_port_range: str = "8100-8149"

    log_ring_lines: int = 2000
    log_max_bytes: int = 5 * 1024 * 1024
    log_backups: int = 2

    stop_grace_seconds: float = 10.0
    install_concurrency: int = 2
    seed_examples: bool = True

    # zip upload limits
    upload_max_zip_bytes: int = 512 * 1024 * 1024
    upload_max_extracted_bytes: int = 2 * 1024 * 1024 * 1024
    upload_max_files: int = 50_000

    # Home Assistant notify relay — tools report notifications to Wharf
    # (POST /api/tools/<id>/notify) and Wharf forwards them to one or more HA
    # notify services (each one a "device" here), which is what's actually
    # wired up to reach a phone.
    ha_url: str = ""              # e.g. http://homeassistant.local:8123
    ha_token: str = ""            # long-lived access token
    ha_notify_devices: list[dict] = Field(default_factory=list)  # [{"id", "label",
    # "service"}, ...] — "service" is the part after "notify." e.g. mobile_app_max_iphone
    ha_default_device_ids: list[str] = Field(default_factory=list)  # device ids notified
    # when a tool/request doesn't name one itself; empty = notify every configured device
    ha_notify_service: str = ""   # bootstrap only: env var HA_NOTIFY_SERVICE from pre-multi
                                   # -device installs, folded into ha_notify_devices below

    # Dashboard login — see app/auth.py. Empty hash = no login wall (default).
    auth_password: str = ""       # bootstrap only: env var AUTH_PASSWORD, hashed into
                                   # auth_password_hash on first boot, never itself persisted
    auth_password_hash: str = ""  # scrypt hash "salt$digest", settings-page editable
    auth_api_token: str = ""      # bearer token for remote/API access when not on loopback

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / ".staging"

    @property
    def pending_updates_dir(self) -> Path:
        """Zip updates staged for an existing tool but not yet applied —
        one subfolder per tool_id, named after it. See app/uploads.py."""
        return self.data_dir / "pending-updates"

    @property
    def port_range(self) -> tuple[int, int]:
        lo, _, hi = self.tools_port_range.partition("-")
        return int(lo), int(hi or lo)

    def ensure_dirs(self) -> None:
        for d in (self.tools_dir, self.logs_dir, self.state_dir, self.staging_dir, self.pending_updates_dir):
            d.mkdir(parents=True, exist_ok=True)


def _overrides_path(state_dir: Path) -> Path:
    return state_dir / "settings_overrides.json"


def load_overrides(state_dir: Path) -> dict:
    path = _overrides_path(state_dir)
    try:
        if path.is_file():
            return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def save_overrides(state_dir: Path, **fields: str) -> None:
    """Persist one or more OVERRIDABLE_FIELDS so they survive a container restart."""
    state_dir.mkdir(parents=True, exist_ok=True)
    path = _overrides_path(state_dir)
    data = load_overrides(state_dir)
    data.update({k: v for k, v in fields.items() if k in OVERRIDABLE_FIELDS})
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    overrides = load_overrides(settings.state_dir)
    for field in OVERRIDABLE_FIELDS:
        if field in overrides:
            setattr(settings, field, overrides[field])
    legacy_service = overrides.get("ha_notify_service") or settings.ha_notify_service
    if legacy_service and not settings.ha_notify_devices:
        # pre-multi-device installs stored a single `ha_notify_service` string (env var
        # HA_NOTIFY_SERVICE or the old settings-page field) — turn it into a one-device
        # list so upgrading doesn't lose the config
        settings.ha_notify_devices = [{"id": "default", "label": "Default", "service": legacy_service}]
        settings.ha_default_device_ids = ["default"]
        save_overrides(
            settings.state_dir,
            ha_notify_devices=settings.ha_notify_devices,
            ha_default_device_ids=settings.ha_default_device_ids,
        )
    if settings.auth_password and not settings.auth_password_hash:
        from . import auth  # local import: auth.py has no reason to import config.py back

        settings.auth_password_hash = auth.hash_password(settings.auth_password)
        save_overrides(settings.state_dir, auth_password_hash=settings.auth_password_hash)
    return settings
