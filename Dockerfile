FROM python:3.12-slim-bookworm

ARG INCLUDE_FFMPEG=true
ARG NODE_MAJOR=22

# System deps: build toolchain for native pip/npm packages, git for git+ deps,
# gosu for privilege drop, tini for PID-1 zombie reaping, procps/unzip for tooling.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg git \
      build-essential pkg-config \
      gosu tini procps unzip \
    && curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && if [ "$INCLUDE_FFMPEG" = "true" ]; then \
         apt-get install -y --no-install-recommends ffmpeg; \
       fi \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Non-root user matching Unraid defaults (nobody:users = 99:100); remapped at runtime.
RUN groupadd -o -g 100 appgroup 2>/dev/null || true \
 && useradd -o -u 99 -g 100 -d /config -m -s /bin/bash app

WORKDIR /opt/hub
COPY pyproject.toml README.md ./
COPY app/ ./app/
RUN uv pip install --system --no-cache .
COPY examples/ /opt/hub/examples/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV DATA_DIR=/data \
    TOOLS_DIR=/data/tools \
    DASHBOARD_PORT=8080 \
    TOOLS_PORT_RANGE=8100-8199 \
    PUID=99 PGID=100 UMASK=022 \
    SEED_EXAMPLES=true \
    UV_LINK_MODE=copy \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8080
VOLUME /data
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
