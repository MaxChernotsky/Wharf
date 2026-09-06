"""Pydantic models: tool.yml manifest schema and runtime records."""

from __future__ import annotations

import enum
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

TOOL_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")


class HealthCheck(BaseModel):
    type: Literal["tcp", "http", "none"] = "tcp"
    path: str = "/"
    timeout_s: float = 60.0


class Manifest(BaseModel):
    """Schema for tool.yml. The folder name is the canonical tool ID; `name` is display-only."""

    name: str | None = None
    description: str = ""
    icon: str = ""
    type: Literal["python", "node", "custom"] | None = None
    install: str | None = None
    run: str
    port: int | None = None
    env: dict[str, str] = Field(default_factory=dict)
    autostart: bool = False
    restart: Literal["never", "on-failure", "always"] = "on-failure"
    max_restarts: int = 5
    health: HealthCheck = Field(default_factory=HealthCheck)
    idle_stop_minutes: int = 0  # stop after N minutes with no open connections (0 = never)
    max_memory_mb: int = 0      # restart if the process tree exceeds this RSS (0 = unlimited)
    watch: bool = False         # dev mode: restart automatically when files in the folder change
    watch_debounce_s: float = 2.0  # quiet period after the last change before restarting
    watch_ignore: list[str] = Field(default_factory=list)  # fnmatch patterns (relative to
    # the tool dir) excluded from the watch fingerprint — for a tool's own runtime output
    # (e.g. "data", "*.sqlite-*") that would otherwise trigger endless self-restarts

    @field_validator("run")
    @classmethod
    def run_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("run command must not be empty")
        return v.strip()

    @field_validator("env", mode="before")
    @classmethod
    def env_values_to_str(cls, v: Any) -> Any:
        if isinstance(v, dict):
            return {str(k): str(val) for k, val in v.items()}
        return v


class ToolStatus(str, enum.Enum):
    NOT_INSTALLED = "not_installed"
    INSTALLING = "installing"
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    UNHEALTHY = "unhealthy"
    CRASHED = "crashed"
    STOPPING = "stopping"
    ERROR = "error"
    UNCONFIGURED = "unconfigured"


class ToolInfo(BaseModel):
    """Everything the UI/API needs to render one tool."""

    id: str
    manifest: Manifest | None = None
    manifest_error: str | None = None
    status: ToolStatus = ToolStatus.STOPPED
    port: int | None = None
    pid: int | None = None
    started_at: float | None = None
    restart_count: int = 0
    last_exit_code: int | None = None
    warnings: list[str] = Field(default_factory=list)
    cpu_pct: float | None = None
    rss_mb: float | None = None
    uptime_s: float | None = None
    stopped_reason: str | None = None
    has_git: bool = False
    git_behind: int | None = None
    git_checked_at: float | None = None
    disk_mb: float | None = None
    spark_cpu: list[float] = Field(default_factory=list)
    spark_rss: list[float] = Field(default_factory=list)

    @property
    def display_name(self) -> str:
        if self.manifest and self.manifest.name:
            return self.manifest.name
        return self.id


class NotificationRecord(BaseModel):
    """One tool-reported notification and the outcome of relaying it to Home
    Assistant — kept in a small on-disk history for the Settings page."""

    id: str
    tool_id: str
    title: str | None = None
    message: str
    priority: str | None = None
    ok: bool
    error: str | None = None
    created_at: float
