"""Per-tool log handling: in-memory ring buffer, rotating file on /data, SSE fanout."""

from __future__ import annotations

import asyncio
import collections
import logging
import re
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

log = logging.getLogger(__name__)

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07")


def strip_ansi(line: str) -> str:
    return _ANSI_RE.sub("", line)


class LogChannel:
    """One stream of lines (e.g. a tool's run output or install output).

    The in-memory ring and SSE subscribers get (timestamp, raw line with ANSI
    colors) so the viewer can render colors and timestamps; the file on disk
    gets the stripped line so it stays grep-friendly.
    """

    def __init__(self, file_path: Path, ring_lines: int, max_bytes: int, backups: int):
        self.ring: collections.deque[tuple[float, str]] = collections.deque(maxlen=ring_lines)
        self.subscribers: set[asyncio.Queue[tuple[float, str]]] = set()
        file_path.parent.mkdir(parents=True, exist_ok=True)
        self._handler = RotatingFileHandler(
            file_path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
        )
        self._logger = logging.Logger(f"tool:{file_path}")
        self._logger.addHandler(self._handler)
        self._logger.propagate = False

    def append(self, line: str) -> None:
        line = line.rstrip("\n")
        entry = (time.time(), line)
        self.ring.append(entry)
        self._logger.info(strip_ansi(line))
        for q in list(self.subscribers):
            try:
                q.put_nowait(entry)
            except asyncio.QueueFull:
                pass  # slow consumer: drop rather than block the reader

    def subscribe(self) -> asyncio.Queue[tuple[float, str]]:
        q: asyncio.Queue[tuple[float, str]] = asyncio.Queue(maxsize=1000)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[tuple[float, str]]) -> None:
        self.subscribers.discard(q)

    def tail(self, lines: int) -> list[tuple[float, str]]:
        return list(self.ring)[-lines:]

    def close(self) -> None:
        self._handler.close()


class LogHub:
    """Registry of LogChannels keyed by (tool_id, source)."""

    def __init__(self, logs_dir: Path, ring_lines: int, max_bytes: int, backups: int):
        self.logs_dir = logs_dir
        self.ring_lines = ring_lines
        self.max_bytes = max_bytes
        self.backups = backups
        self._channels: dict[tuple[str, str], LogChannel] = {}

    def channel(self, tool_id: str, source: str = "run") -> LogChannel:
        key = (tool_id, source)
        if key not in self._channels:
            self._channels[key] = LogChannel(
                self.logs_dir / tool_id / f"{source}.log",
                self.ring_lines,
                self.max_bytes,
                self.backups,
            )
        return self._channels[key]

    def drop(self, tool_id: str) -> None:
        for key in [k for k in self._channels if k[0] == tool_id]:
            self._channels.pop(key).close()
