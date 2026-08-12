import json

from app.ports import PortAllocator


def make_alloc(tmp_path, lo=8100, hi=8104):
    return PortAllocator(lo, hi, tmp_path / "ports.json")


def test_auto_assign_is_sticky(tmp_path):
    a = make_alloc(tmp_path)
    p1 = a.resolve("tool-a", None)
    assert p1 == 8100
    assert a.resolve("tool-a", None) == p1
    # survives reload from disk
    b = make_alloc(tmp_path)
    assert b.resolve("tool-a", None) == p1


def test_pinned_port_wins_and_persists(tmp_path):
    a = make_alloc(tmp_path)
    assert a.resolve("tool-a", 8103) == 8103
    assert json.loads((tmp_path / "ports.json").read_text())["tool-a"] == 8103


def test_auto_assign_skips_taken_ports(tmp_path):
    a = make_alloc(tmp_path)
    a.resolve("pinned", 8100)
    assert a.resolve("auto", None) == 8101


def test_release_frees_port(tmp_path):
    a = make_alloc(tmp_path)
    a.resolve("t", None)
    a.release("t")
    assert a.resolve("u", None) == 8100


def test_range_exhaustion_raises(tmp_path):
    a = make_alloc(tmp_path, 8100, 8101)
    a.resolve("a", None)
    a.resolve("b", None)
    import pytest
    with pytest.raises(RuntimeError):
        a.resolve("c", None)


def test_set_range_prunes_out_of_range_auto_assignments(tmp_path):
    a = make_alloc(tmp_path)  # 8100-8104
    a.resolve("auto", None)  # 8100
    a.resolve("pinned", 9999)  # out of range even before the move
    a.set_range(9000, 9010, pinned_ids={"pinned"})
    assert "auto" not in a.assigned
    assert a.assigned["pinned"] == 9999
    assert a.resolve("auto", None) == 9000


def test_set_range_keeps_assignments_still_in_range(tmp_path):
    a = make_alloc(tmp_path)
    a.resolve("auto", None)  # 8100
    a.set_range(8090, 8110, pinned_ids=set())
    assert a.assigned["auto"] == 8100


def test_is_free_detects_listener(tmp_path):
    import socket
    with socket.socket() as s:
        s.bind(("0.0.0.0", 0))
        s.listen(1)
        port = s.getsockname()[1]
        assert PortAllocator.is_free(port) is False
    assert PortAllocator.is_free(port) is True
