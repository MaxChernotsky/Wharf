"""FastAPI application: wiring + lifespan (boot scan, autostart, graceful shutdown)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .api import routes_auth, routes_logs, routes_notifications, routes_tools, routes_ui
from .auth import AuthMiddleware, LoginLockout, SessionSigner
from .config import get_settings
from .gitops import UpdateMonitor
from .installer import Installer
from .logbuf import LogHub
from .manager import ProcessManager
from .notifications import NotificationHub
from .ports import PortAllocator
from .resources import ResourceMonitor
from .watcher import FolderWatcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.ensure_dirs()

    loghub = LogHub(
        settings.logs_dir,
        settings.log_ring_lines,
        settings.log_max_bytes,
        settings.log_backups,
    )
    lo, hi = settings.port_range
    ports = PortAllocator(lo, hi, settings.state_dir / "ports.json")
    manager = ProcessManager(settings, loghub, ports)
    installer = Installer(manager)
    notifications = NotificationHub(settings)

    app.state.settings = settings
    app.state.loghub = loghub
    app.state.manager = manager
    app.state.installer = installer
    app.state.notifications = notifications
    app.state.session_signer = SessionSigner(settings.state_dir)
    app.state.login_lockout = LoginLockout()

    await manager.boot()
    monitor = ResourceMonitor(manager)
    monitor.start()
    git_monitor = UpdateMonitor(manager)
    git_monitor.start()
    folder_watcher = FolderWatcher(manager)
    folder_watcher.start()
    log.info(
        "Wharf up — %d tools, port range %s",
        len(manager.entries),
        settings.tools_port_range,
    )
    yield
    log.info("shutting down: stopping all tools")
    monitor.stop()
    git_monitor.stop()
    folder_watcher.stop()
    await manager.shutdown()


app = FastAPI(title="Wharf", lifespan=lifespan)
app.add_middleware(AuthMiddleware)

app.include_router(routes_tools.router)
app.include_router(routes_logs.router)
app.include_router(routes_notifications.router)
app.include_router(routes_auth.router)
app.include_router(routes_auth.settings_router)
app.include_router(routes_ui.router)
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
    name="static",
)


@app.get("/healthz")
def healthz():
    return {"ok": True}
