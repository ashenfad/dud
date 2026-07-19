"""Direct tests for the wire layer everything else stands on.

Uses socketpairs with a threaded peer where a live counterparty is
needed; truncation cases just write raw bytes and close.
"""

from __future__ import annotations

import socket
import struct
import threading

import pytest

from dud.proto import (
    Channel,
    ChannelClosed,
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
