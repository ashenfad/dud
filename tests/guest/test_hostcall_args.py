"""HostProxy argument encoding, including the size guard.

A hostcall argument crosses in the JSON body of a frame the supervisor
parses whole, exactly as a harvested binding does — so it needs the
same ceiling. It refuses at the call rather than dropping, because a
hostcall is something agent code asked for by name: passing the host
object a truncated argument, or silently none, would make it do the
wrong thing quietly.
"""

from __future__ import annotations

import pytest

from dud.guest.runner import HostProxy


class _RecordingChannel:
    def __init__(self):
        self.sent = []

    def request(self, verb, body, bins=None):
        self.sent.append((verb, body))
        return {"result": {"t": "json", "v": "ok"}}, []


def test_an_ordinary_argument_still_crosses():
    ch = _RecordingChannel()
    assert HostProxy("db", ch, cap=1_000).query("select 1", limit=5) == "ok"
    verb, body = ch.sent[0]
    assert verb == "hostcall" and body["obj"] == "db" and body["method"] == "query"
    assert body["args"] == [{"t": "json", "v": "select 1"}]
    assert body["kwargs"] == {"limit": {"t": "json", "v": 5}}


def test_an_oversized_positional_argument_is_refused():
    ch = _RecordingChannel()
    with pytest.raises(TypeError) as e:
        HostProxy("db", ch, cap=1_000).query("z" * 5_000)
    assert "per-value limit" in str(e.value)
    assert ch.sent == [], "the oversized argument reached the wire anyway"


def test_an_oversized_keyword_argument_is_refused():
    ch = _RecordingChannel()
    with pytest.raises(TypeError):
        HostProxy("db", ch, cap=1_000).query(blob="z" * 5_000)
    assert ch.sent == []


def test_the_unencodable_case_still_reports_its_type():
    """The guard shares a path with representability, and must not have
    turned "this has no encoding" into a size complaint."""
    ch = _RecordingChannel()
    with pytest.raises(TypeError) as e:
        HostProxy("db", ch, cap=1_000).query(object())
    assert "object" in str(e.value)


def test_no_cap_leaves_the_old_behavior():
    ch = _RecordingChannel()
    HostProxy("db", ch).query("z" * 5_000)
    assert len(ch.sent) == 1


def test_many_legal_arguments_are_refused_in_aggregate():
    """Each argument under the ceiling, the frame far over it: 20 args
    of 7 MiB pass an 8 MiB per-value check and assemble into 140 MB,
    which the supervisor would parse whole. The channel's own send
    ceiling is what catches it; this pins that the error names the
    method rather than surfacing as a bare wire failure."""
    import socket
    import threading

    from dud.proto import Channel

    a, b = socket.socketpair()
    # A real responder on the far end, not a sink. Without one, removing
    # the ceiling makes this HANG rather than fail: the body goes out and
    # `request` then waits forever for a reply. A test whose regression
    # signal is a hang is not a regression signal.
    peer = Channel(b, handler=lambda v, body, bins: ({"result": {"t": "json",
                                                                 "v": "ok"}}, []))
    server = threading.Thread(target=peer.serve, daemon=True)
    server.start()
    try:
        ch = Channel(a, send_cap=10_000)
        with pytest.raises(TypeError) as e:
            HostProxy("db", ch, cap=5_000).query(*["z" * 4_000 for _ in range(10)])
        assert "db.query" in str(e.value)
        assert "aggregate" in str(e.value)
    finally:
        a.close()
        b.close()
        server.join(timeout=2)
