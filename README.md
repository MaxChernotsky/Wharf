# ⚓ Wharf

A single Docker container that hosts and manages all your self-made AI tools —
Python and Node apps that each serve a web UI on a port. Built for Unraid, works
anywhere Docker runs.

- **One container** — Python 3.12 + Node 22 + [uv](https://github.com/astral-sh/uv) baked in.
- **A dashboard** on port `8080` to install, start, stop, restart, and tail logs for every tool.
- **Each tool** lives in its own folder on the appdata share with its own `.venv` / `node_modules`,
  gets a dedicated port from a mapped range (`8100–8149`, up to 50 tools at once), and is supervised with
  crash-restart policies.

## Quick start (local)

```bash
docker compose up --build
```

Open http://localhost:8080. A single example tool — `port-scanner` — is seeded on
first run and starts immediately; it scans every localhost port and reports what's
listening on each.

## Adding a tool

**Option A — drop a folder into the share.** Copy your tool's folder into
`tools/` inside the appdata share, click **Rescan** in the dashboard, then
**Configure** — a suggested `tool.yml` is prefilled for you to review.

**Option B — upload a zip, or drag-and-drop a folder** from the **Add tool**
page. Dropping a folder sends its files individually (each carrying its
relative path) and Wharf reconstructs it server-side — useful when you don't
want to zip something up first.

**Option C — link a local folder, for active development.** From the
**Settings** page (or `POST /api/tools/link` with `path`/`name` form fields),
point Wharf at a folder that already exists on disk instead of copying it in.
Wharf creates a symlink at `tools/<id>` pointing at it, so nothing is
duplicated — you keep editing the tool in its own checkout, in your own
editor, and Wharf runs whatever's actually there. `watch` (see below) defaults
to on for a freshly-linked tool with no `tool.yml` yet, since the whole point
of linking is picking up edits automatically. The folder just needs to
already be visible to the Wharf process itself: a local path when running
Wharf locally for dev, or a path bind-mounted into the container in Docker.
Deleting a linked tool only ever removes the symlink — the folder it points
to is never touched, even with "delete files" checked.

On the Settings page, **Browse…** next to the path field pops a native
Finder folder picker instead of typing the path by hand (`POST
/api/tools/browse`) — it only works when Wharf itself is running locally on
macOS with a GUI session (it shells out to `osascript`), since the dialog has
to appear on the machine actually running the Wharf process. It fails with a
clear error on a headless/Docker/Linux host; type the path there instead.

**Option D — clone from a git URL**, from the **Add tool** page or
`POST /api/tools/clone` (form fields `url`, `name`). The tool ID defaults to
the repo name. See **Git-backed tools** below for what you get afterwards.

### tool.yml

The folder name is the tool's ID. Everything except `run` is optional:

```yaml
name: Whisper Transcriber
description: Drag-and-drop audio transcription
icon: "🎙️"
type: python                # python | node | custom (auto-detected if omitted)
install: uv pip install -r requirements.txt
run: streamlit run app.py --server.port {port} --server.address 0.0.0.0
port: 8101                  # omit to auto-assign from the range (sticky)
env:
  MODEL_SIZE: base
autostart: true             # start when the container boots
restart: on-failure         # never | on-failure | always
max_restarts: 5             # within a 10-minute window
health:
  type: http                # tcp | http | none
  path: /
  timeout_s: 120            # give slow model loads headroom
idle_stop_minutes: 30       # stop after N minutes with no open connections (0 = never)
max_memory_mb: 2048         # restart if the process tree exceeds this RSS (0 = unlimited)
watch: false                 # dev mode: restart automatically when files in the folder change
watch_debounce_s: 2          # quiet period after the last change before restarting
notify_devices: []           # device ids from Settings → Notifications that this tool's
                              # /notify calls go to by default (empty = use the Settings default)
```

Idle-stopped tools wake on demand: `http://<host>:8080/launch/<tool>` starts the
tool (if needed), waits for it to become healthy, and redirects — a bookmarkable
"always works" URL for every tool.

### Dev mode: auto-restart on change

Set `watch: true` and the dashboard polls the tool's folder every couple of
seconds while it's running. Once file changes go quiet for `watch_debounce_s`,
it restarts the tool automatically — no manual Restart click needed while
you're actively editing a tool's code. It's polling-based (not inotify),
because appdata shares are commonly edited over SMB or a FUSE mount where
filesystem-event notifications don't reliably arrive. `.git`, `.venv`,
`node_modules`, and similar generated dirs are ignored. Toggle it from the
dashboard (a 👀 button next to Install/Restart) or via
`POST /api/tools/<tool>/watch?enabled=true`.

Conventions:

- `{port}` and `{dir}` are substituted into `run`, `install`, and `env` values.
- `PORT`, `HOST=0.0.0.0`, `TOOL_NAME`, `TOOL_DIR` are always injected into the environment.
- For Python tools the tool's `.venv/bin` is prepended to `PATH` — no activate needed.
- **Your tool must bind `0.0.0.0`**, not `127.0.0.1`, or it's unreachable from your network.
  The dashboard detects this and shows a warning.

### Git-backed tools

A tool cloned via Option D (or any tool folder that happens to contain a
`.git` dir) gets extra treatment on its detail page:

- **Status** — current branch, dirty/clean, last commit, and remote URL
  (`GET /api/tools/<id>/git`).
- **Update checks** — a background scan fetches every cloned tool's remote
  every 30 minutes and caches how far local `HEAD` is ahead/behind upstream,
  so the dashboard can flag available updates without a network call on every
  page view. Trigger an on-demand check with `POST /api/tools/<id>/git/check`.
- **Pull** — `POST /api/tools/<id>/git/pull` runs `git pull --ff-only`, then
  reinstalls dependencies and restarts the tool if it was running (skipped if
  already up to date).

### Monitoring and export

Each running tool's CPU% and RSS memory are sampled every 5 seconds (plus
per-tool disk usage roughly once a minute), feeding both the detail-page
charts and the `idle_stop_minutes` / `max_memory_mb` enforcement above.
History and events: `GET /api/tools/<id>/stats`.

Download a tool's folder as a zip (dependencies and `.git` excluded) from the
detail page, or `GET /api/tools/<id>/export`.

## Authentication

Wharf's dashboard and API are unauthenticated by default. Since you can start,
delete, and run arbitrary code for every tool from here, that's fine on a
trusted LAN but worth locking down before exposing Wharf externally.

Set a password from **Settings → Technical → Authentication** (or the
`AUTH_PASSWORD` environment variable, hashed into settings on first boot —
same bootstrap-then-persist pattern as the notify config below) and a
`/login` page starts gating every request. Three things stay reachable
without logging in:

- **Requests from the same host as Wharf itself** — tools already call
  Wharf's own API over loopback (e.g. the notify relay below), so nothing
  about existing tools needs to change.
- **`/launch/<tool>`** — kept open so a bookmark or iOS Shortcut that hits it
  keeps working.
- **`/healthz`** — the container healthcheck.

For everything else, a browser needs a signed-in session; a script or remote
agent (e.g. one building a tool through the API from off-host — see
**Building a tool through the API** below) can instead send
`Authorization: Bearer <token>` using the API token generated on the same
Settings page.

This only protects Wharf's own dashboard/API on `DASHBOARD_PORT`. Each
hosted tool is still its own unauthenticated web server on its assigned port
— Wharf's login doesn't extend to them.

### Forgot the password?

There's no in-app recovery flow by design — a single-user, self-hosted tool
has no email/SMS to recover through. To reset it: on the appdata share, edit
`state/settings_overrides.json` (e.g. `/mnt/user/appdata/wharf/state/settings_overrides.json`
on Unraid) and remove the `auth_password_hash` key, then restart the
container. That drops the dashboard back to unauthenticated, exactly like a
fresh install — set a new password from Settings once it's back up.

## Notifications

Wharf can act as a single notification service for every tool it hosts: a
tool calls Wharf's own API to report something worth telling you about, and
Wharf relays it to a [Home Assistant](https://www.home-assistant.io/) `notify`
service — typically the mobile-app notify service HA already sets up for
your phone once the HA Companion App is installed and paired. Wharf never
talks to your phone directly; Home Assistant is the actual delivery channel,
so anything HA's notify service supports (actionable notifications, critical
alerts on iOS, etc.) works here too via the optional `data` field.

Configure it once from **Settings → Technical → Notifications**:

- **Home Assistant URL** — e.g. `http://homeassistant.local:8123`.
- **Long-lived access token** — create one from your HA user profile's
  **Security** tab. Saved fields round-trip without re-entering the token on
  every edit; check **Clear saved token** to remove it.
- **Devices** — add one row per notify target: a **label** (whatever you
  want to call it) and a **notify service**, the part after `notify.` for
  the HA service that reaches it. Find service names in HA under
  **Developer Tools → Actions**, searching for `notify.` — each per-device
  mobile-app service (e.g. `mobile_app_max_iphone`) reaches one phone. Check
  **Default** on the device(s) that should get a notification when neither
  the tool nor the notify call itself names one.

A **Send test notification** button on the same page confirms the whole path
end to end. `HA_URL` and `HA_TOKEN` can be set as environment variables so a
fresh container comes up partly configured; `HA_NOTIFY_SERVICE` bootstraps a
single device the same way on first boot (add more from the Settings page
afterwards). Anything saved from the Settings page persists to
`/data/state/settings_overrides.json` (plaintext, same trust model as the
rest of Wharf's unauthenticated dashboard) and wins over the environment.

From a tool, send a notification with:

```bash
curl -s -X POST http://$HOST/api/tools/<your-tool-id>/notify \
  -H 'Content-Type: application/json' \
  -d '{"message": "render finished", "title": "My Tool"}'
```

Which device(s) receive it, in order: the request's own `device` (one id) or
`devices` (a list of ids) field, else the tool's own `notify_devices` in
`tool.yml` (also settable from a checklist on the tool's detail page), else
the default device(s) checked on the Settings page. `title` and `priority`
are optional; `priority` is passed through to HA as `data.priority` unless
you already set `data.priority` yourself. Any other key under `data` is
forwarded to HA's notify service as-is — use it for things like
`data.actions` (actionable notification buttons) or `data.push.sound` on
iOS. The endpoint returns `502` with the underlying error if Home Assistant
is unreachable, misconfigured, or rejects the request, so a tool can tell
the difference between "sent" and "not sent" without polling. Recent
notifications (across all tools, with which device(s) each one went to) are
visible on the Settings page, or at `GET /api/notifications` (add
`?tool_id=` to filter).

## Building a tool through the API (for AI agents)

Wharf is meant to be the *only* place a tool ever runs — including while it's
being built. An AI coding agent developing a new tool doesn't need local
filesystem access to wherever `tools_dir` actually lives (it may be inside a
remote container) and doesn't need to `pip install`/`npm install` and run
anything itself to check its work: it writes files, installs, starts, and
reads logs entirely over Wharf's JSON API, the same way the dashboard does.
All endpoints below are under `http://<host>:8080`.

| Step | Endpoint |
|---|---|
| Create a tool from inline files | `POST /api/tools` — `{"tool_id", "files": {"path": "content"}, "manifest"?}` |
| Add/edit one file | `PUT /api/tools/<id>/files/<path>` — `{"content"}` (first write creates the tool) |
| Read a file back | `GET /api/tools/<id>/files/<path>` |
| List a tool's files | `GET /api/tools/<id>/files` |
| Delete a file | `DELETE /api/tools/<id>/files/<path>` |
| Link an existing local folder instead (no copy) | `POST /api/tools/link` — form fields `path`, `name` — see Option C above; only works if `path` is visible to the Wharf process itself |
| Clone from a git URL instead | `POST /api/tools/clone` — form fields `url`, `name` — see Option D above |
| Git status / check for updates / pull | `GET /api/tools/<id>/git`, `POST /api/tools/<id>/git/check`, `POST /api/tools/<id>/git/pull` |
| CPU/RAM history + events | `GET /api/tools/<id>/stats` |
| Download a tool as a zip | `GET /api/tools/<id>/export` |
| Save/replace tool.yml | `PUT /api/tools/<id>/manifest` — full `Manifest` JSON body |
| Get a best-effort tool.yml draft | `POST /api/tools/<id>/manifest/suggest` |
| Install dependencies (backgrounded) | `POST /api/tools/<id>/install` |
| Poll install progress | `GET /api/tools/<id>/install/status` |
| Start / stop / restart | `POST /api/tools/<id>/{start,stop,restart}` |
| Turn on auto-restart while iterating | `POST /api/tools/<id>/watch?enabled=true` |
| Send a notification (relayed to Home Assistant) | `POST /api/tools/<id>/notify` — `{"message", "title"?, "priority"?, "data"?, "device"?, "devices"?}` — see **Notifications** below |
| Set which notification device(s) a tool defaults to | `POST /api/tools/<id>/notify-devices` — `{"devices": [id, ...]}` |
| Tail recent logs | `GET /api/logs/<id>/tail?lines=200&source=run` (`source=install` for install output) |
| Stream logs live | `GET /api/logs/<id>/stream` (SSE) |
| Full status (port, pid, health, warnings) | `GET /api/tools/<id>` |
| Tear down | `DELETE /api/tools/<id>?delete_files=true` |

A typical build-and-iterate session:

```bash
HOST=localhost:8080

# 1. scaffold — writes files and tool.yml in one call
curl -s -X POST http://$HOST/api/tools -H 'Content-Type: application/json' -d '{
  "tool_id": "word-counter",
  "files": {
    "app.py": "from fastapi import FastAPI\napp = FastAPI()\n@app.get(\"/\")\ndef root(): return {\"ok\": True}\n",
    "requirements.txt": "fastapi\nuvicorn\n"
  },
  "manifest": {
    "name": "Word Counter",
    "run": "uvicorn app:app --host 0.0.0.0 --port {port}",
    "health": {"type": "http", "path": "/"}
  }
}'

# 2. install deps, then poll until it finishes
curl -s -X POST http://$HOST/api/tools/word-counter/install
curl -s http://$HOST/api/tools/word-counter/install/status

# 3. start it and check status/health
curl -s -X POST http://$HOST/api/tools/word-counter/start
curl -s http://$HOST/api/tools/word-counter

# 4. read logs to confirm it's actually serving
curl -s "http://$HOST/api/logs/word-counter/tail?lines=100"

# 5. turn on watch mode, then keep editing via PUT .../files/... —
#    each save restarts the tool on its own within watch_debounce_s
curl -s -X POST "http://$HOST/api/tools/word-counter/watch?enabled=true"
curl -s -X PUT http://$HOST/api/tools/word-counter/files/app.py \
  -H 'Content-Type: application/json' \
  -d '{"content": "from fastapi import FastAPI\napp = FastAPI()\n@app.get(\"/\")\ndef root(): return {\"ok\": True, \"v\": 2}\n"}'
curl -s "http://$HOST/api/logs/word-counter/tail?lines=20"   # should show the restart
```

Full request/response shapes are in the auto-generated OpenAPI docs at
`/docs` (Swagger UI) and `/openapi.json`.

## Unraid setup

Add a container using the template in `unraid/wharf.xml`, or manually:

| Setting | Value |
|---|---|
| Port | `8080 → 8080` (dashboard WebUI) |
| Port | `8100-8149 → 8100-8149` (tool range, up to 50 tools — **must be 1:1 same-numbered**) |
| Path | `/data` → `/mnt/user/appdata/wharf` |
| Variable | `PUID=99`, `PGID=100`, `UMASK=022` |
| Variable | `TOOLS_PORT_RANGE=8100-8149` |
| Extra params | `--stop-timeout 30` |

The port range must be mapped 1:1 with the same numbers because the dashboard's
**Open** links point at `http://<host>:<container-port>`. Alternatively, run the
container with **host networking** — then no port mappings are needed and any
range works, at the cost of sharing the host's port namespace.

Tools then live at `/mnt/user/appdata/wharf/tools/<name>/`, editable over SMB.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `DASHBOARD_PORT` | `8080` | Dashboard listen port |
| `TOOLS_PORT_RANGE` | `8100-8149` | Ports assignable to tools (50 by default) |
| `PUID` / `PGID` | `99` / `100` | Owner of files created on `/data` |
| `UMASK` | `022` | umask for the dashboard and all tools |
| `SEED_EXAMPLES` | `true` | Copy example tools into an empty tools dir |
| `STOP_GRACE_SECONDS` | `10` | SIGTERM→SIGKILL grace per tool |
| `INSTALL_CONCURRENCY` | `2` | Max parallel install jobs |
| `HA_URL` | *(none)* | Home Assistant base URL for notification relay — see **Notifications** |
| `HA_TOKEN` | *(none)* | Home Assistant long-lived access token |
| `HA_NOTIFY_SERVICE` | *(none)* | Bootstraps a single Home Assistant notify device on first boot — add more from **Settings → Notifications** afterwards |
| `AUTH_PASSWORD` | *(none)* | Bootstraps the dashboard login on first boot — see **Authentication** |

## Development

```bash
uv venv && uv pip install -e '.[dev]'
uv run pytest
uv run uvicorn app.main:app --reload   # needs DATA_DIR pointing somewhere writable
```

## How it works

The dashboard (FastAPI) supervises each tool as a subprocess in its own process
group: stop sends SIGTERM to the whole group and escalates to SIGKILL after a
grace period, so wrapper chains like `npm start` die cleanly. Running pids are
persisted to `/data/state/running.json`, and on boot any orphans from a crashed
dashboard are reaped. Installs run as background jobs (`uv` for Python, `npm`
for Node) with output streamed to the log viewer via SSE.
