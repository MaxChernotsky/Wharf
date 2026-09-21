import asyncio

import pytest

from app.logbuf import LogHub
from app.manager import ProcessManager
from app.models import ToolStatus
from app.ports import PortAllocator


def make_manager(settings) -> ProcessManager:
    hub = LogHub(settings.logs_dir, 500, 1024 * 1024, 1)
    lo, hi = settings.port_range
    ports = PortAllocator(lo, hi, settings.state_dir / "ports.json")
    return ProcessManager(settings, hub, ports)


def make_tool(settings, name: str, manifest: str, files: dict | None = None):
    d = settings.tools_dir / name
    d.mkdir(parents=True)
    (d / "tool.yml").write_text(manifest)
    for fname, content in (files or {}).items():
        (d / fname).write_text(content)
    return d


HTTP_SERVER = (
    "import http.server, os\n"
    "port = int(os.environ['PORT'])\n"
    "http.server.HTTPServer(('0.0.0.0', port), http.server.SimpleHTTPRequestHandler).serve_forever()\n"
)


@pytest.mark.asyncio
async def test_start_and_stop_http_tool(settings):
    make_tool(settings, "srv",
              "run: python3 server.py\nhealth: {type: tcp, timeout_s: 15}\n",
              {"server.py": HTTP_SERVER})
    mgr = make_manager(settings)
    mgr.rescan()

    ok, msg = await mgr.start_tool("srv")
    assert ok, msg
    for _ in range(60):
        if mgr.tool_info("srv").status == ToolStatus.RUNNING:
            break
        await asyncio.sleep(0.25)
    info = mgr.tool_info("srv")
    assert info.status == ToolStatus.RUNNING
    assert info.pid

    ok, _ = await mgr.stop_tool("srv")
    assert ok
    assert mgr.tool_info("srv").status == ToolStatus.STOPPED
    # running.json cleared
    assert mgr._load_running() == {}


@pytest.mark.asyncio
async def test_crash_gets_restarted_then_gives_up(settings):
    make_tool(settings, "crashy",
              "run: python3 -c 'import sys; sys.exit(1)'\n"
              "restart: on-failure\nmax_restarts: 1\nhealth: {type: none}\n")
    mgr = make_manager(settings)
    mgr.rescan()
    ok, msg = await mgr.start_tool("crashy")
    assert ok, msg
    sup = mgr.supervisor("crashy")
    for _ in range(80):
        if sup.error and "gave up" in sup.error:
            break
        await asyncio.sleep(0.25)
    assert sup.status == ToolStatus.CRASHED
    assert sup.error and "gave up" in sup.error


@pytest.mark.asyncio
async def test_clean_exit_with_never_policy_stays_stopped(settings):
    make_tool(settings, "oneshot",
              "run: python3 -c 'print(42)'\nrestart: never\nhealth: {type: none}\n")
    mgr = make_manager(settings)
    mgr.rescan()
    ok, msg = await mgr.start_tool("oneshot")
    assert ok, msg
    for _ in range(40):
        if mgr.supervisor("oneshot").status == ToolStatus.STOPPED:
            break
        await asyncio.sleep(0.25)
    assert mgr.supervisor("oneshot").status == ToolStatus.STOPPED
    assert mgr.supervisor("oneshot").last_exit_code == 0


@pytest.mark.asyncio
async def test_start_unconfigured_tool_fails(settings):
    (settings.tools_dir / "bare").mkdir()
    mgr = make_manager(settings)
    mgr.rescan()
    ok, msg = await mgr.start_tool("bare")
    assert not ok


@pytest.mark.asyncio
async def test_apply_port_range_restarts_running_tools_on_new_range(settings):
    make_tool(settings, "srv",
              "run: python3 server.py\nhealth: {type: tcp, timeout_s: 15}\n",
              {"server.py": HTTP_SERVER})
    mgr = make_manager(settings)
    mgr.rescan()
    ok, msg = await mgr.start_tool("srv")
    assert ok, msg
    for _ in range(60):
        if mgr.tool_info("srv").status == ToolStatus.RUNNING:
            break
        await asyncio.sleep(0.25)
    old_port = mgr.tool_info("srv").port
    assert 18100 <= old_port <= 18110

    await mgr.apply_port_range(18200, 18210)
    assert settings.tools_port_range == "18200-18210"

    for _ in range(60):
        if mgr.tool_info("srv").status == ToolStatus.RUNNING:
            break
        await asyncio.sleep(0.25)
    info = mgr.tool_info("srv")
    assert info.status == ToolStatus.RUNNING
    assert 18200 <= info.port <= 18210
    await mgr.stop_tool("srv")


@pytest.mark.asyncio
async def test_process_group_kill_reaps_children(settings):
    # parent bash spawns a child sleep; stopping must kill both
    make_tool(settings, "nested",
              'run: bash -c "sleep 300 & wait"\nhealth: {type: none}\nrestart: never\n')
    mgr = make_manager(settings)
    mgr.rescan()
    ok, msg = await mgr.start_tool("nested")
    assert ok, msg
    await asyncio.sleep(0.5)
    sup = mgr.supervisor("nested")
    pgid = sup.pgid
    assert pgid
    await mgr.stop_tool("nested")
    await asyncio.sleep(0.5)
    import os
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


def test_tool_info_reports_pending_update(settings):
    make_tool(settings, "srv", "run: echo hi\n")
    mgr = make_manager(settings)
    mgr.rescan()
    assert mgr.tool_info("srv").has_pending_update is False

    (settings.pending_updates_dir / "srv").mkdir(parents=True)
    assert mgr.tool_info("srv").has_pending_update is True


def test_tool_info_reports_linked(settings, tmp_path):
    dev_dir = tmp_path / "dev-checkout"
    dev_dir.mkdir()
    (dev_dir / "tool.yml").write_text("run: echo hi\n")
    (settings.tools_dir / "linked").symlink_to(dev_dir, target_is_directory=True)
    mgr = make_manager(settings)
    mgr.rescan()
    assert mgr.tool_info("linked").is_linked is True
