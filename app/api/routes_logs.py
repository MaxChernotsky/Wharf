"""SSE log streaming and tail endpoints."""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from ..logbuf import LogHub, strip_ansi
from .deps import get_loghub, get_manager

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/logs")

VALID_SOURCES = {"run", "install"}


@router.get("/{tool_id}/tail")
def tail(tool_id: str, lines: int = 500, source: str = "run", hub: LogHub = Depends(get_loghub)):
    if source not in VALID_SOURCES:
        raise HTTPException(400, "source must be 'run' or 'install'")
    return {"lines": [strip_ansi(s) for _, s in hub.channel(tool_id, source).tail(lines)]}


@router.get("/{tool_id}/download")
def download(
    tool_id: str,
    source: str = "run",
    hub: LogHub = Depends(get_loghub),
    mgr=Depends(get_manager),
):
    if source not in VALID_SOURCES:
        raise HTTPException(400, "source must be 'run' or 'install'")
    if not mgr.entry(tool_id):
        mgr.rescan()
        if not mgr.entry(tool_id):
            raise HTTPException(404, f"no tool named {tool_id!r}")
    path = hub.logs_dir / tool_id / f"{source}.log"
    if not path.exists():
        raise HTTPException(404, "no log file yet")
    return FileResponse(path, media_type="text/plain", filename=f"{tool_id}-{source}.log")


@router.get("/{tool_id}/stream")
async def stream(
    tool_id: str,
    request: Request,
    source: str = "run",
    replay: int = 500,
    hub: LogHub = Depends(get_loghub),
    mgr=Depends(get_manager),
):
    if source not in VALID_SOURCES:
        raise HTTPException(400, "source must be 'run' or 'install'")
    if not mgr.entry(tool_id):
        mgr.rescan()
        if not mgr.entry(tool_id):
            raise HTTPException(404, f"no tool named {tool_id!r}")

    chan = hub.channel(tool_id, source)

    MAX_STREAM_S = 600  # cap stream lifetime; EventSource reconnects and gets a fresh replay

    async def event_gen():
        q = chan.subscribe()
        deadline = asyncio.get_event_loop().time() + MAX_STREAM_S
        try:
            for entry in chan.tail(replay):
                yield _sse(entry)
            while asyncio.get_event_loop().time() < deadline:
                if await request.is_disconnected():
                    return
                try:
                    entry = await asyncio.wait_for(q.get(), timeout=15)
                    yield _sse(entry)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            chan.unsubscribe(q)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(entry: tuple[float, str]) -> str:
    # one JSON object per event: {"t": epoch, "s": raw line (may contain ANSI)}
    ts, line = entry
    payload = json.dumps({"t": round(ts, 3), "s": line}, ensure_ascii=False)
    return f"data: {payload}\n\n"
