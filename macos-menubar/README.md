# Wharf menubar

A tiny macOS menubar app (⚓) that polls a Wharf dashboard's `GET /api/tools`
and lists whatever's currently active, so you can jump straight to a tool
without opening the dashboard first.

- Polls every 5s; shows a 🟢/🟡/🟠 dot for running/starting/unhealthy tools.
- Click a tool to open it in your browser of choice (**Open with** submenu —
  defaults to your system default browser).
- Uses each tool's own port directly when known, falling back to the
  dashboard's `/launch/<id>` wake-up URL otherwise.
- Host and browser choice are saved to `~/.wharf_menubar.json`.

This is a standalone script — separate from the Wharf server itself (which
runs in Docker) — so it gets its own tiny venv rather than living in the
main project's dependencies.

## Setup

```bash
cd macos-menubar
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

```bash
.venv/bin/python wharf_menubar.py
```

An anchor icon (⚓) appears in the menu bar. Click it to see active tools,
change the Wharf host (default `localhost:8080`), or pick a browser.

## Run at login (optional)

Create `~/Library/LaunchAgents/com.wharf.menubar.plist`, filling in the
absolute paths for your checkout:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.wharf.menubar</string>
  <key>ProgramArguments</key>
  <array>
    <string>/absolute/path/to/Wharf/macos-menubar/.venv/bin/python</string>
    <string>/absolute/path/to/Wharf/macos-menubar/wharf_menubar.py</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
</dict>
</plist>
```

Then load it:

```bash
launchctl load ~/Library/LaunchAgents/com.wharf.menubar.plist
```

Unload with `launchctl unload ~/Library/LaunchAgents/com.wharf.menubar.plist`.

## Not done here

Packaging as a proper double-clickable `.app` (e.g. via `py2app`) so it
doesn't need a venv/terminal at all — straightforward to add later if it's
worth it, but the script above covers day-to-day use.
