import asyncio
import socket

import pytest

from app import procutil
from app.ports import PortAllocator


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.asyncio
async def test_find_port_pid_and_reap_port_kill_an_untracked_listener():
    """Simulates the real-world failure this exists for: a process is bound to
    a tool's port that Wharf has no record of (crash, killed dashboard, a
    process it simply lost track of) — reap_port must locate and kill it."""
    port = _free_port()
    # start_new_session=True: matches how manager.py spawns real tools, and
    # keeps this dummy listener out of pytest's own process group — reap_port
    # kills a whole pgid, and without this it would kill the test runner too.
    proc = await asyncio.create_subprocess_exec(
        "python3", "-c",
        f"import socket,time; s=socket.socket(); s.bind(('0.0.0.0',{port})); "
        f"s.listen(1); time.sleep(30)",
        start_new_session=True,
    )
    try:
        for _ in range(50):
            if not PortAllocator.is_free(port):
                break
            await asyncio.sleep(0.1)
        assert not PortAllocator.is_free(port), "listener never came up"

        assert procutil.find_port_pid(port) == proc.pid

        reaped = await procutil.reap_port(port, grace_seconds=2.0)
        assert reaped == proc.pid
        assert PortAllocator.is_free(port)
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()


@pytest.mark.asyncio
async def test_reap_port_is_noop_on_a_free_port():
    port = _free_port()
    assert await procutil.reap_port(port, grace_seconds=1.0) is None
