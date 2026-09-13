#!/bin/bash
# Wharf entrypoint: remap uid/gid for Unraid, prepare /data, drop privileges.
set -euo pipefail

PUID="${PUID:-99}"
PGID="${PGID:-100}"
UMASK="${UMASK:-022}"
DATA_DIR="${DATA_DIR:-/data}"

echo "[entrypoint] PUID=${PUID} PGID=${PGID} UMASK=${UMASK} DATA_DIR=${DATA_DIR}"

# Remap the app user/group to the requested ids (idempotent).
current_gid="$(getent group appgroup | cut -d: -f3 || echo '')"
if [ "$current_gid" != "$PGID" ]; then
    groupmod -o -g "$PGID" appgroup
fi
current_uid="$(id -u app 2>/dev/null || echo '')"
if [ "$current_uid" != "$PUID" ]; then
    usermod -o -u "$PUID" app
fi

umask "$UMASK"

# Skeleton dirs. Chown only the skeleton + top-level entries whose owner mismatches;
# never blind-recursive chown (appdata can be huge).
mkdir -p "$DATA_DIR/tools" "$DATA_DIR/logs" "$DATA_DIR/state" "$DATA_DIR/.staging"
for d in "$DATA_DIR" "$DATA_DIR/tools" "$DATA_DIR/logs" "$DATA_DIR/state" "$DATA_DIR/.staging"; do
    if [ "$(stat -c '%u:%g' "$d")" != "${PUID}:${PGID}" ]; then
        chown "${PUID}:${PGID}" "$d"
    fi
done
find "$DATA_DIR/tools" -mindepth 1 -maxdepth 1 ! -user "$PUID" -exec chown -R "${PUID}:${PGID}" {} + 2>/dev/null || true
find "$DATA_DIR/logs" "$DATA_DIR/state" -mindepth 1 ! -user "$PUID" -exec chown "${PUID}:${PGID}" {} + 2>/dev/null || true

# Seed example tools that aren't already present. Runs on every start (not just
# when tools/ is empty) so tools added to examples/ later still show up for
# existing installs, without touching tools a user already has.
if [ "${SEED_EXAMPLES:-true}" = "true" ]; then
    for example_dir in /opt/hub/examples/*/; do
        [ -d "$example_dir" ] || continue
        example_name="$(basename "$example_dir")"
        if [ ! -e "$DATA_DIR/tools/$example_name" ]; then
            echo "[entrypoint] seeding example tool: $example_name"
            cp -r "$example_dir" "$DATA_DIR/tools/$example_name"
            chown -R "${PUID}:${PGID}" "$DATA_DIR/tools/$example_name"
        fi
    done
fi

echo "[entrypoint] starting dashboard on :${DASHBOARD_PORT:-8080} as uid ${PUID}"
# --timeout-graceful-shutdown: open SSE log streams must not block the lifespan
# shutdown that stops the tools when the container receives SIGTERM.
exec gosu app python -m uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "${DASHBOARD_PORT:-8080}" \
    --app-dir /opt/hub \
    --timeout-graceful-shutdown 3 \
    --no-access-log
