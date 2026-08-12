"""Settings loaded from environment variables (set by Dockerfile / Unraid template),
with a small set of fields user-overridable at runtime from the settings page."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings

# fields the settings UI is allowed to persist over the environment defaults
OVERRIDABLE_FIELDS = {"tools_port_range"}


class Settings(BaseSettings):
    data_dir: Path = Path("/data")
    tools_dir: Path = Path("/data/tools")
    dashboard_port: int = 8080
    tools_port_range: str = "8100-8199"

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
    def port_range(self) -> tuple[int, int]:
        lo, _, hi = self.tools_port_range.partition("-")
        return int(lo), int(hi or lo)

    def ensure_dirs(self) -> None:
        for d in (self.tools_dir, self.logs_dir, self.state_dir, self.staging_dir):
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
    if "tools_port_range" in overrides:
        settings.tools_port_range = overrides["tools_port_range"]
    return settings
