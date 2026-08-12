import asyncio

import pytest

from app.logbuf import LogHub
from app.manager import ProcessManager
from app.ports import PortAllocator
from app.watcher import FolderWatcher, describe_diff, fingerprint


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


def test_fingerprint_skips_noise_dirs_and_junk_files(tmp_path):
    (tmp_path / "app.py").write_text("x")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "lib.py").write_text("y")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.js").write_text("z")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "app.cpython-312.pyc").write_text("bin")
    (tmp_path / "app.log").write_text("noisy")
    (tmp_path / ".DS_Store").write_text("junk")

    snap = fingerprint(tmp_path)
    assert set(snap) == {"app.py"}


def test_fingerprint_tracks_dotfiles_like_env(tmp_path):
    (tmp_path / ".env").write_text("SECRET=1")
    assert ".env" in fingerprint(tmp_path)


def test_describe_diff_added_removed_edited():
    old = {"a.py": (1, 10)}
    assert "added b.py" in describe_diff(old, {"a.py": (1, 10), "b.py": (2, 5)})
    assert "removed a.py" in describe_diff(old, {})
    assert "edited a.py" in describe_diff(old, {"a.py": (2, 10)})
    assert describe_diff(old, dict(old)) == ""


SLEEPER = "run: python3 -c \"import time; time.sleep(300)\"\nhealth: {type: none}\nrestart: never\n"


@pytest.mark.asyncio
async def test_watcher_restarts_after_debounced_change(settings):
    make_tool(
        settings, "watched",
        SLEEPER + "watch: true\nwatch_debounce_s: 0.15\n",
        {"app.py": "print(1)\n"},
    )
    mgr = make_manager(settings)
    mgr.rescan()
    ok, msg = await mgr.start_tool("watched")
    assert ok, msg
    sup = mgr.supervisor("watched")
    for _ in range(40):
        if sup.proc is not None:
            break
        await asyncio.sleep(0.1)
    first_pid = sup.proc.pid

    watcher = FolderWatcher(mgr)
    await watcher._tick()  # baseline snapshot, no restart
    assert sup.proc.pid == first_pid
    assert "watched" in watcher._state and watcher._state["watched"].snapshot

    # edit a tracked file — size changes so the diff is unambiguous
    (settings.tools_dir / "watched" / "app.py").write_text("print(1000000)\n")
    await watcher._tick()  # detects the change, starts the debounce clock
    assert sup.proc.pid == first_pid  # not yet — still within the debounce window

    await asyncio.sleep(0.4)
    await watcher._tick()  # quiet for long enough: restarts now

    for _ in range(60):
        if sup.proc and sup.proc.pid != first_pid:
            break
        await asyncio.sleep(0.1)
    assert sup.proc.pid != first_pid
    assert any(kind == "watch-restart" for _, kind, _ in sup.events)

    await mgr.stop_tool("watched")


@pytest.mark.asyncio
async def test_watcher_ignores_tools_without_watch_flag(settings):
    make_tool(settings, "plain", SLEEPER, {"app.py": "print(1)\n"})
    mgr = make_manager(settings)
    mgr.rescan()
    ok, msg = await mgr.start_tool("plain")
    assert ok, msg
    sup = mgr.supervisor("plain")
    for _ in range(40):
        if sup.proc is not None:
            break
        await asyncio.sleep(0.1)
    first_pid = sup.proc.pid

    watcher = FolderWatcher(mgr)
    await watcher._tick()
    assert "plain" not in watcher._state

    (settings.tools_dir / "plain" / "app.py").write_text("print(2)\n")
    await watcher._tick()
    await asyncio.sleep(0.3)
    await watcher._tick()
    assert sup.proc.pid == first_pid

    await mgr.stop_tool("plain")
