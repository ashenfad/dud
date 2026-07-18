"""Wire protocol: length-prefixed JSON frames with binary attachments.

One channel, both directions, zero dependencies. A message is:

    [4-byte BE length][JSON bytes]

The JSON object carries ``id`` (per-sender monotonic int), ``kind``
(``req`` / ``resp`` / ``err``), ``verb`` (requests only), ``body``
(dict), and ``nbin`` — the count of binary frames that immediately
follow, each ``[4-byte BE length][raw bytes]``. Binary frames carry
payloads that would be wasteful as base64 (workspace tars, diff tars).

Both ends are synchronous. Either side may initiate a request; while a
sender is blocked awaiting its response it services incoming requests
from the other side (via its ``handler``). This is what lets a guest
runner call ``cache.get`` / ``hostcall`` *during* the host's
``exec_python`` request: the host sits in ``request()``, pumping and
answering guest requests until its own response arrives. Requests and
responses from the two directions cannot collide: each side matches
responses only against ids it issued.

The protocol is versioned via the ``hello`` exchange (see
``PROTO_VERSION``); mismatches fail loud at connect, not weird later.
"""

from __future__ import annotations

import json
import socket
import struct
from typing import Callable

PROTO_VERSION = 1

_LEN = struct.Struct(">I")

# A handler takes (verb, body, bins) and returns (body, bins).
Handler = Callable[[str, dict, list[bytes]], tuple[dict, list[bytes]]]


class ProtocolError(Exception):
    """Framing or handshake violation. The channel is unusable after."""


class RemoteError(Exception):
    """The other side answered a request with ``kind: err``."""

    def __init__(self, verb: str, message: str, etype: str = "RemoteError"):
        super().__init__(f"{verb}: [{etype}] {message}")
        self.verb = verb
        self.etype = etype
        self.message = message


class ChannelClosed(Exception):
    """EOF on the socket."""


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ChannelClosed()
        buf.extend(chunk)
    return bytes(buf)


class Channel:
    """Bidirectional request/response over a stream socket.

    ``handler`` services requests initiated by the other side. It may
    be None for a pure client that never receives requests (then an
    incoming request is a protocol error).
    """

    def __init__(self, sock: socket.socket, handler: Handler | None = None):
        self._sock = sock
        self.handler = handler
        self._next_id = 0

    # ---- framing ----------------------------------------------------

    def _send_msg(self, msg: dict, bins: list[bytes]) -> None:
        msg = dict(msg, nbin=len(bins))
        data = json.dumps(msg, separators=(",", ":")).encode()
        out = bytearray(_LEN.pack(len(data)) + data)
        for b in bins:
            out += _LEN.pack(len(b)) + b
        self._sock.sendall(out)

    def _recv_msg(self) -> tuple[dict, list[bytes]]:
        (n,) = _LEN.unpack(_recv_exact(self._sock, 4))
        msg = json.loads(_recv_exact(self._sock, n).decode())
        bins = []
        for _ in range(int(msg.get("nbin", 0))):
            (bn,) = _LEN.unpack(_recv_exact(self._sock, 4))
            bins.append(_recv_exact(self._sock, bn))
        return msg, bins

    # ---- handshake ---------------------------------------------------

    def hello_send(self) -> None:
        self._send_msg({"kind": "hello", "proto": PROTO_VERSION}, [])

    def hello_recv(self) -> None:
        msg, _ = self._recv_msg()
        if msg.get("kind") != "hello":
            raise ProtocolError(f"expected hello, got {msg.get('kind')!r}")
        if msg.get("proto") != PROTO_VERSION:
            raise ProtocolError(
                f"protocol version mismatch: peer {msg.get('proto')}, "
                f"local {PROTO_VERSION}"
            )

    # ---- request/response -------------------------------------------

    def request(
        self, verb: str, body: dict | None = None, bins: list[bytes] | None = None
    ) -> tuple[dict, list[bytes]]:
        """Send a request; pump incoming requests until our response."""
        self._next_id += 1
        rid = self._next_id
        self._send_msg(
            {"id": rid, "kind": "req", "verb": verb, "body": body or {}},
            bins or [],
        )
        while True:
            msg, mbins = self._recv_msg()
            kind = msg.get("kind")
            if kind == "req":
                self._serve_one(msg, mbins)
            elif kind == "resp" and msg.get("id") == rid:
                return msg.get("body", {}), mbins
            elif kind == "err" and msg.get("id") == rid:
                raise RemoteError(
                    verb, msg.get("message", ""), msg.get("etype", "RemoteError")
                )
            else:
                raise ProtocolError(f"unexpected frame: {kind!r} id={msg.get('id')}")

    def _serve_one(self, msg: dict, bins: list[bytes]) -> None:
        rid, verb = msg.get("id"), msg.get("verb", "")
        if self.handler is None:
            self._send_msg(
                {"id": rid, "kind": "err", "etype": "ProtocolError",
                 "message": f"no handler for {verb!r}"},
                [],
            )
            return
        try:
            rbody, rbins = self.handler(verb, msg.get("body", {}), bins)
            self._send_msg({"id": rid, "kind": "resp", "body": rbody}, rbins)
        except _Shutdown:
            self._send_msg({"id": rid, "kind": "resp", "body": {}}, [])
            raise
        except Exception as e:  # noqa: BLE001 — boundary: report, don't die
            self._send_msg(
                {"id": rid, "kind": "err", "etype": type(e).__name__,
                 "message": str(e)},
                [],
            )

    def serve(self) -> None:
        """Serve incoming requests until shutdown or EOF."""
        try:
            while True:
                msg, bins = self._recv_msg()
                if msg.get("kind") != "req":
                    raise ProtocolError(f"server got {msg.get('kind')!r}")
                self._serve_one(msg, bins)
        except (_Shutdown, ChannelClosed):
            return

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


class _Shutdown(Exception):
    """Raised by a handler to end ``serve()`` after responding."""


def shutdown_served() -> None:
    """Handlers call this on the ``shutdown`` verb (responds, then exits)."""
    raise _Shutdown()
