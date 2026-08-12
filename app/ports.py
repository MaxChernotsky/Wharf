"""Port allocation: manifest-pinned or auto-assigned from the mapped range, persisted."""

from __future__ import annotations

import json
import logging
import os
import socket
from pathlib import Path

log = logging.getLogger(__name__)


class PortAllocator:
    def __init__(self, range_lo: int, range_hi: int, state_file: Path):
        self.lo = range_lo
        self.hi = range_hi
        self.state_file = state_file
        self.assigned: dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        try:
            if self.state_file.is_file():
                raw = json.loads(self.state_file.read_text())
                self.assigned = {str(k): int(v) for k, v in raw.items()}
        except (json.JSONDecodeError, ValueError, OSError) as e:
            log.warning("could not load %s: %s", self.state_file, e)
            self.assigned = {}

    def _save(self) -> None:
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.assigned, indent=2))
        os.replace(tmp, self.state_file)

    def in_range(self, port: int) -> bool:
        return self.lo <= port <= self.hi

    def resolve(self, tool_id: str, pinned: int | None) -> int:
        """Return the port for a tool: manifest pin wins, else sticky auto-assignment."""
        if pinned is not None:
            if self.assigned.get(tool_id) != pinned:
                self.assigned[tool_id] = pinned
                self._save()
            return pinned
        if tool_id in self.assigned:
            return self.assigned[tool_id]
        taken = set(self.assigned.values())
        for port in range(self.lo, self.hi + 1):
            if port not in taken:
                self.assigned[tool_id] = port
                self._save()
                return port
        raise RuntimeError(f"no free ports left in range {self.lo}-{self.hi}")

    def release(self, tool_id: str) -> None:
        if self.assigned.pop(tool_id, None) is not None:
            self._save()

    def set_range(self, lo: int, hi: int, pinned_ids: set[str]) -> None:
        """Change the allocatable range and drop any non-pinned sticky
        assignment that now falls outside it, so those tools get a fresh
        port from the new range next time they're resolved."""
        self.lo, self.hi = lo, hi
        stale = [
            tool_id for tool_id, port in self.assigned.items()
            if tool_id not in pinned_ids and not self.in_range(port)
        ]
        for tool_id in stale:
            del self.assigned[tool_id]
        if stale:
            self._save()

    @staticmethod
    def is_free(port: int) -> bool:
        """Bind test: True if nothing is currently listening on the port."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("0.0.0.0", port))
                return True
            except OSError:
                return False
