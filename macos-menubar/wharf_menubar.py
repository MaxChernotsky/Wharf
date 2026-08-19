#!/usr/bin/env python3
"""macOS menubar app for Wharf: lists active tools, opens one in your browser
of choice with a click. Polls `GET /api/tools` on a Wharf dashboard and uses
its `/launch/<id>` URL so a tool that's idle-stopped wakes up on open too."""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import rumps

CONFIG_PATH = Path.home() / ".wharf_menubar.json"
DEFAULT_HOST = "localhost:8080"
POLL_INTERVAL_S = 5
REQUEST_TIMEOUT_S = 3

ACTIVE_STATUSES = {"running", "starting", "unhealthy"}
STATUS_DOT = {"running": "🟢", "starting": "🟡", "unhealthy": "🟠"}

BROWSER_CHOICES = [
    "Default Browser",
    "Safari",
    "Google Chrome",
    "Firefox",
    "Microsoft Edge",
    "Brave Browser",
    "Arc",
]


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    except OSError:
        pass


class WharfMenubarApp(rumps.App):
    def __init__(self):
        super().__init__("⚓", quit_button=None)
        cfg = load_config()
        self.host = cfg.get("host", DEFAULT_HOST)
        self.browser = cfg.get("browser", "Default Browser")
        self._tool_items: list[rumps.MenuItem] = []

        self.status_item = rumps.MenuItem("Checking Wharf…")

        self.browser_menu = rumps.MenuItem("Open with")
        for name in BROWSER_CHOICES:
            item = rumps.MenuItem(name, callback=self.set_browser)
            item.state = name == self.browser
            self.browser_menu.add(item)

        self.menu = [
            self.status_item,
            rumps.separator,
            self.browser_menu,
            rumps.MenuItem("Set Wharf Host…", callback=self.set_host),
            rumps.MenuItem("Refresh Now", callback=self.refresh_now),
            rumps.separator,
            rumps.MenuItem("Quit Wharf Menubar", callback=rumps.quit_application),
        ]

        # the menu's dict key for an item is fixed at insertion time and does
        # NOT follow later `.title =` renames, so capture it now rather than
        # re-reading self.status_item.title (which we rename every refresh)
        self._status_key = self.status_item.title

        self.timer = rumps.Timer(self.refresh, POLL_INTERVAL_S)
        self.timer.start()
        self.refresh(None)

    def hostname(self) -> str:
        return self.host.split(":")[0]

    def fetch_tools(self) -> list[dict] | None:
        url = f"http://{self.host}/api/tools"
        try:
            with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT_S) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
            return None

    def refresh(self, _sender):
        self._rebuild_tool_items(self.fetch_tools())

    def refresh_now(self, _sender):
        self.refresh(None)

    def _rebuild_tool_items(self, tools: list[dict] | None):
        for item in self._tool_items:
            del self.menu[item.title]
        self._tool_items = []

        if tools is None:
            self.status_item.title = f"⚠️ Can't reach Wharf at {self.host}"
            self.title = "⚓⚠️"
            return

        active = sorted(
            (t for t in tools if t.get("status") in ACTIVE_STATUSES),
            key=lambda t: ((t.get("manifest") or {}).get("name") or t["id"]).lower(),
        )
        self.status_item.title = f"{len(active)} active tool{'' if len(active) == 1 else 's'}"
        self.title = "⚓" if active else "⚓"

        anchor = self._status_key
        if not active:
            placeholder = rumps.MenuItem("No active tools")
            self.menu.insert_after(anchor, placeholder)
            self._tool_items.append(placeholder)
            return

        for tool in active:
            manifest = tool.get("manifest") or {}
            icon = manifest.get("icon") or "▫️"
            name = manifest.get("name") or tool["id"]
            dot = STATUS_DOT.get(tool.get("status"), "⚪️")
            item = rumps.MenuItem(f"{icon} {name} {dot}", callback=self._make_opener(tool))
            self.menu.insert_after(anchor, item)
            anchor = item.title
            self._tool_items.append(item)

    def _make_opener(self, tool: dict):
        port = tool.get("port")
        tool_id = tool["id"]
        url = f"http://{self.hostname()}:{port}/" if port else f"http://{self.host}/launch/{tool_id}"

        def opener(_sender):
            self.open_url(url)

        return opener

    def open_url(self, url: str):
        if self.browser == "Default Browser":
            subprocess.run(["open", url])
        else:
            subprocess.run(["open", "-a", self.browser, url])

    def set_browser(self, sender):
        for item in self.browser_menu.values():
            item.state = False
        sender.state = True
        self.browser = sender.title
        save_config({"host": self.host, "browser": self.browser})

    def set_host(self, _sender):
        window = rumps.Window(
            message="Wharf dashboard host (host:port):",
            title="Set Wharf Host",
            default_text=self.host,
            ok="Save",
            cancel="Cancel",
        )
        response = window.run()
        if response.clicked and response.text.strip():
            self.host = response.text.strip()
            save_config({"host": self.host, "browser": self.browser})
            self.refresh(None)


if __name__ == "__main__":
    WharfMenubarApp().run()
