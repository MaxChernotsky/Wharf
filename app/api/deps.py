"""Shared accessors for objects stored on app.state by the lifespan."""

from __future__ import annotations

from fastapi import Request

from ..config import Settings
from ..installer import Installer
from ..logbuf import LogHub
from ..manager import ProcessManager
from ..notifications import NotificationHub


def get_manager(request: Request) -> ProcessManager:
    return request.app.state.manager


def get_installer(request: Request) -> Installer:
    return request.app.state.installer


def get_loghub(request: Request) -> LogHub:
    return request.app.state.loghub


def get_config(request: Request) -> Settings:
    return request.app.state.settings


def get_notifications(request: Request) -> NotificationHub:
    return request.app.state.notifications
