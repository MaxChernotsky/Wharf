"""Health probing (tcp/http) and loopback-bind detection via /proc/net/tcp."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .models import HealthCheck

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 2.0


async def _tcp_up(port: int) -> bool:
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout=2
        )
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass
        return True
    except (OSError, asyncio.TimeoutError):
        return False


async def _http_up(port: int, path: str) -> bool:
    """Minimal HTTP GET over asyncio — any response with status < 500 counts as up."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout=2
        )
        writer.write(
            f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n".encode()
        )
        await writer.drain()
        status_line = await asyncio.wait_for(reader.readline(), timeout=5)
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass
        parts = status_line.decode("latin-1").split()
        return len(parts) >= 2 and parts[1].isdigit() and int(parts[1]) < 500
    except (OSError, asyncio.TimeoutError, ValueError):
        return False


async def probe(port: int, hc: HealthCheck) -> bool:
    if hc.type == "http":
        return await _http_up(port, hc.path)
    return await _tcp_up(port)


async def wait_healthy(port: int, hc: HealthCheck, *, single_shot: bool = False) -> bool:
    """Poll until healthy or timeout. single_shot=True does one probe only."""
    if hc.type == "none":
        return True
    if single_shot:
        return await probe(port, hc)
    deadline = asyncio.get_event_loop().time() + hc.timeout_s
    while asyncio.get_event_loop().time() < deadline:
        if await probe(port, hc):
            return True
        await asyncio.sleep(POLL_INTERVAL_S)
    return False


def check_loopback_bind(port: int) -> str | None:
    """Return '127.0.0.1' / '::1' if the listener on `port` is loopback-only, else None.

    Parses /proc/net/tcp and /proc/net/tcp6 (Linux only; returns None elsewhere).
    A wildcard (0.0.0.0 / ::) or specific non-loopback listener means reachable.
    """
    listeners: list[str] = []
    for proc_file, loopback_hex, any_hex in (
        (Path("/proc/net/tcp"), "0100007F", "00000000"),
        (Path("/proc/net/tcp6"), "00000000000000000000000001000000", "0" * 32),
    ):
        try:
            lines = proc_file.read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 4 or fields[3] != "0A":  # 0A = LISTEN
                continue
            addr_hex, _, port_hex = fields[1].partition(":")
            if int(port_hex, 16) != port:
                continue
            if addr_hex == any_hex:
                return None  # wildcard bind — reachable
            if addr_hex == loopback_hex:
                listeners.append("127.0.0.1" if len(addr_hex) == 8 else "::1")
            else:
                return None  # bound to a specific non-loopback address
    return listeners[0] if listeners else None
