"""Direct tests for the wire layer everything else stands on.

Uses socketpairs with a threaded peer where a live counterparty is
needed; truncation cases just write raw bytes and close.
"""

from __future__ import annotations

import socket
import struct
import threading
import time

import pytest

from dud.proto import (
    Channel,
    ChannelClosed,
    FrameTooLarge,
    ProtocolError,
    PROTO_VERSION,
    RemoteError,
    shutdown_served,
)

_LEN = struct.Struct(">I")


def _pair() -> tuple[Channel, Channel]:
    a, b = socket.socketpair()
    return Channel(a), Channel(b)


def _served(handler):
    """A channel whose peer serves ``handler`` in a thread until shutdown."""
    a, b = socket.socketpair()
    server = Channel(b, handler=handler)
    t = threading.Thread(target=server.serve, daemon=True)
    t.start()
    return Channel(a), t


def test_request_roundtrip_with_binary_frames():
    def handler(verb, body, bins):
        assert verb == "echo"
        return {"got": body["x"], "nbins": len(bins)}, [b"resp-bin"]

    client, t = _served(handler)
    body, bins = client.request("echo", {"x": 41}, [b"a", b"b"])
    assert body == {"got": 41, "nbins": 2}
    assert bins == [b"resp-bin"]
    client.close()
    t.join(timeout=5)


def test_handler_exception_becomes_remote_error():
    def handler(verb, body, bins):
        raise KeyError("boom")

    client, t = _served(handler)
    with pytest.raises(RemoteError) as ei:
        client.request("explode")
    assert ei.value.etype == "KeyError"
    client.close()
    t.join(timeout=5)


def test_shutdown_verb_ends_serve_after_responding():
    def handler(verb, body, bins):
        if verb == "shutdown":
            shutdown_served()
        return {}, []

    client, t = _served(handler)
    body, _ = client.request("shutdown")  # still gets a response
    assert body == {}
    t.join(timeout=5)
    assert not t.is_alive()
    client.close()


def test_hello_version_mismatch_fails_loud():
    a, b = _pair()
    b._send_msg({"kind": "hello", "proto": PROTO_VERSION + 1}, [])
    with pytest.raises(ProtocolError, match="version mismatch"):
        a.hello_recv()


def test_hello_wrong_kind_fails_loud():
    a, b = _pair()
    b._send_msg({"kind": "req", "verb": "run"}, [])
    with pytest.raises(ProtocolError, match="expected hello"):
        a.hello_recv()


def test_truncated_header_raises_channel_closed():
    a, b = socket.socketpair()
    b.sendall(b"\x00\x00")  # half a length prefix
    b.close()
    with pytest.raises(ChannelClosed):
        Channel(a)._recv_msg()


def test_truncated_payload_raises_channel_closed():
    a, b = socket.socketpair()
    b.sendall(_LEN.pack(100) + b"{}")  # promises 100 bytes, sends 2
    b.close()
    with pytest.raises(ChannelClosed):
        Channel(a)._recv_msg()


def test_truncated_binary_frame_raises_channel_closed():
    a, b = socket.socketpair()
    payload = b'{"kind":"resp","id":1,"nbin":1}'
    b.sendall(_LEN.pack(len(payload)) + payload + _LEN.pack(50) + b"xx")
    b.close()
    with pytest.raises(ChannelClosed):
        Channel(a)._recv_msg()


def test_unexpected_frame_mid_request_is_protocol_error():
    a, b = socket.socketpair()
    client = Channel(a)

    def peer():
        server = Channel(b)
        server._recv_msg()  # swallow the request
        server._send_msg({"kind": "resp", "id": 999}, [])  # wrong id

    t = threading.Thread(target=peer, daemon=True)
    t.start()
    with pytest.raises(ProtocolError, match="unexpected frame"):
        client.request("anything")
    t.join(timeout=5)
    client.close()


def test_handlerless_channel_rejects_incoming_requests():
    a, b = socket.socketpair()
    client = Channel(a)  # no handler

    def peer():
        server = Channel(b)
        server._recv_msg()  # client's request
        # Fire a counter-request; the handlerless client must err it,
        # then we answer the original request.
        server._send_msg({"id": 1, "kind": "req", "verb": "intrude"}, [])
        msg, _ = server._recv_msg()
        assert msg["kind"] == "err" and msg["etype"] == "ProtocolError"
        server._send_msg({"kind": "resp", "id": 1, "body": {"ok": 1}}, [])

    t = threading.Thread(target=peer, daemon=True)
    t.start()
    body, _ = client.request("hi")
    assert body == {"ok": 1}
    t.join(timeout=5)
    client.close()


def test_serve_reports_why_it_stopped():
    """serve() distinguishes shutdown / freeze / EOF: freeze must be an
    explicit, acked verb — a bare EOF keeps meaning "die" so the
    no-dangling-VMs invariant survives the snapshot work."""
    from dud.proto import freeze_served

    def handler(verb, body, bins):
        if verb == "freeze":
            freeze_served()
        shutdown_served()

    # freeze: the requester gets an ack AND serve() returns "freeze"
    a, b = socket.socketpair()
    server = Channel(b, handler=handler)
    reasons = []
    t = threading.Thread(target=lambda: reasons.append(server.serve()),
                         daemon=True)
    t.start()
    client = Channel(a)
    body, _ = client.request("freeze")
    assert body == {}
    t.join(timeout=5)
    assert reasons == ["freeze"]
    client.close()

    # shutdown
    a, b = socket.socketpair()
    server = Channel(b, handler=handler)
    reasons = []
    t = threading.Thread(target=lambda: reasons.append(server.serve()),
                         daemon=True)
    t.start()
    Channel(a).request("shutdown")
    t.join(timeout=5)
    assert reasons == ["shutdown"]
    a.close()

    # EOF
    a, b = socket.socketpair()
    server = Channel(b, handler=handler)
    a.close()
    assert server.serve() == "eof"


# ---- send ceiling -------------------------------------------------------


def test_send_cap_refuses_an_oversized_body():
    """The structural backstop. Per-value guards can only bound what
    they were pointed at; names, argument counts and any path nobody
    thought to guard all funnel through here anyway."""
    a, b = socket.socketpair()
    try:
        ch = Channel(a, send_cap=1000)
        with pytest.raises(FrameTooLarge) as e:
            ch._send_msg({"kind": "req", "verb": "x", "body": {"v": "z" * 5000}}, [])
        assert "nothing was sent" in str(e.value)
        # And nothing was: the peer has no frame waiting.
        b.settimeout(0.1)
        with pytest.raises((TimeoutError, OSError)):
            b.recv(1)
    finally:
        a.close()
        b.close()


def test_send_cap_ignores_binary_attachments():
    """Attachments are read as opaque bytes rather than parsed, and
    capping them here would quietly make cache writes a wire question
    when their size is a question about cache semantics."""
    a, b = socket.socketpair()
    try:
        # Comfortably over the cap, comfortably under the socket buffer:
        # nobody is draining the far end, so a payload big enough to
        # fill it would block in sendall and hang the test rather than
        # fail it.
        Channel(a, send_cap=100)._send_msg({"kind": "req", "verb": "x"},
                                            [b"z" * 4_000])
        assert b.recv(4)  # it went out
    finally:
        a.close()
        b.close()


def test_no_send_cap_by_default():
    """The host side sets none: a body the host composed is not a
    payload anyone needs protecting from."""
    a, b = socket.socketpair()
    try:
        assert Channel(a).send_cap is None
    finally:
        a.close()
        b.close()


# ---- cancel ---------------------------------------------------------------


def _serving_until_cancelled(gate: threading.Event | None = None):
    """A peer whose ``work`` verb waits for the frame a cancel sends and
    answers with whether it counted as a cancel of ``work``. With
    ``gate``, it reads nothing until the gate opens."""
    a, b = socket.socketpair()
    server = Channel(b)

    def handler(verb, body, bins):
        if verb == "work":
            server.read_cancel()
            return {"cancelled": server.cancel_requested()}, []
        if verb == "echo":
            return dict(body, cancelled=server.cancel_requested()), []
        shutdown_served()

    def serve():
        if gate is not None:
            gate.wait()
        server.serve()

    server.handler = handler
    threading.Thread(target=serve, daemon=True).start()
    return Channel(a)


def test_a_cancel_reaches_the_request_being_served():
    client = _serving_until_cancelled()
    box = {}
    t = threading.Thread(
        target=lambda: box.update(r=client.request("work", timeout=10))
    )
    t.start()
    while client.awaiting is None:
        pass
    assert client.cancel()
    t.join(timeout=10)
    assert box["r"][0] == {"cancelled": True}
    assert client.awaiting is None
    client.request("shutdown")


def test_a_cancel_naming_another_request_is_not_this_ones():
    """A cancel that loses the race with its answer names an older
    request, and must not stop the one served now."""
    client = _serving_until_cancelled()
    box = {}
    t = threading.Thread(
        target=lambda: box.update(r=client.request("work", timeout=10))
    )
    t.start()
    while client.awaiting is None:
        pass
    client._send_msg({"kind": "cancel", "target": client.awaiting - 1}, [])
    t.join(timeout=10)
    assert box["r"][0] == {"cancelled": False}
    client.request("shutdown")


def test_a_late_cancel_is_dropped_by_an_idle_server():
    client = _serving_until_cancelled()
    client._send_msg({"kind": "cancel", "target": 1}, [])
    assert client.request("echo", {"v": 1}, timeout=10)[0] == {
        "v": 1, "cancelled": False
    }
    client.request("shutdown")


def test_cancel_with_nothing_awaited_sends_nothing():
    a, b = socket.socketpair()
    try:
        assert Channel(a).cancel() is False
        b.settimeout(0.1)
        with pytest.raises((TimeoutError, OSError)):
            b.recv(1)
    finally:
        a.close()
        b.close()


def test_a_cancel_during_the_requests_send_is_not_missed():
    """A request too big for the socket's buffer is still being sent
    when the cancel comes, and the peer may already be serving it: the
    cancel finds it, and goes out after it rather than before."""
    gate = threading.Event()
    client = _serving_until_cancelled(gate)
    box = {}
    pad = "x" * 4_000_000  # far past the buffer: the send blocks
    sender = threading.Thread(
        target=lambda: box.update(r=client.request("work", {"pad": pad}, timeout=10)),
        daemon=True,
    )
    sender.start()
    time.sleep(0.3)  # the send is blocked on a peer not reading yet
    canceller = threading.Thread(
        target=lambda: box.update(sent=client.cancel()), daemon=True
    )
    canceller.start()
    time.sleep(0.1)
    gate.set()
    canceller.join(timeout=10)
    sender.join(timeout=10)
    assert box["sent"] is True
    assert box["r"][0] == {"cancelled": True}
    client.request("shutdown")
