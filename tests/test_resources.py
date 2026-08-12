import os
import socket

from app.resources import established_counts, pgroup_stats


def test_pgroup_stats_includes_own_group():
    stats = pgroup_stats()
    assert stats, "ps sweep returned nothing"
    own_pgid = os.getpgid(0)
    assert own_pgid in stats
    cpu, rss_mb = stats[own_pgid]
    assert rss_mb > 0


def test_established_counts_sees_live_connection():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port)) as client:
            conn, _ = server.accept()
            with conn:
                counts = established_counts({port})
                assert counts[port] >= 1
    assert established_counts({port})[port] == 0


def test_established_counts_empty_ports():
    assert established_counts(set()) == {}
