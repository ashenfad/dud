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
import time

from .errors import DudError
from typing import Callable

PROTO_VERSION = 1

# Why this rarely moves, and why a wire change usually shouldn't move it:
# host and guest cannot be different builds. `inject_dud` installs the
# host's own dud package into the rootfs, and `_spec_hash` includes
# `_dud_code_hash()` — a digest of exactly those injected files. Editing
# anything the guest runs therefore changes the image spec, so the host
# looks up (and builds) a different rootfs rather than reusing one
# holding the older guest. Verified: this file's own edits move the spec
# hash.
#
# So the skew this version guards against — a new host meeting an old
# guest — has no path through the normal build. It stays because the
# handshake is cheap and a hand-assembled rootfs, or a future rung that
# rents a machine it did not build, would reintroduce the possibility.
# Bump it when the framing itself changes shape (the header format, the
# attachment protocol), not merely because a verb's payload moved.

_LEN = struct.Struct(">I")

# A handler takes (verb, body, bins) and returns (body, bins).
Handler = Callable[[str, dict, list[bytes]], tuple[dict, list[bytes]]]


class ProtocolError(DudError):
    """Framing or handshake violation. The channel is unusable after."""


class RemoteError(DudError):
    """The other side answered a request with ``kind: err``."""

    def __init__(self, verb: str, message: str, etype: str = "RemoteError"):
        super().__init__(f"{verb}: [{etype}] {message}")
        self.verb = verb
        self.etype = etype
        self.message = message


class ChannelClosed(Exception):
    """EOF on the socket."""


class FrameTooLarge(DudError):
    """A message body exceeded this channel's send ceiling.

    Distinct from :class:`~dud.values.ValueTooLarge`, which bounds one
    value: this bounds the whole JSON object a peer will have to parse,
    and so covers everything a per-value check cannot see — the names
    beside the values, and the aggregate of many individually-legal
    ones. Both exist because the guest's peer is a supervisor that is
    PID 1 on a VM rung, and per-value limits turned out to be a
    guarantee about payloads rather than about frames.

    The channel stays usable: nothing was written.
    """


def _arm(sock: socket.socket, deadline: float | None) -> None:
    """Point the socket at the remaining budget before a blocking call.

    Armed per operation rather than once per request because a request
    is many blocking calls (a send, then a recv per frame, then a recv
    per attachment), and each has to see what is *left* of the budget
    rather than the whole of it. ``None`` leaves the socket blocking,
    which is what every guest-side channel wants: the guest imposes no
    deadline on the host, and ``serve()`` waits indefinitely for the
    next request by design.
    """
    if deadline is None:
        return
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("deadline exceeded")
    sock.settimeout(remaining)


def _recv_exact(
    sock: socket.socket, n: int, deadline: float | None = None
) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        _arm(sock, deadline)
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

    def __init__(self, sock: socket.socket, handler: Handler | None = None,
                 send_cap: int | None = None):
        self._sock = sock
        self.handler = handler
        self._next_id = 0
        # Ceiling on the JSON body of anything sent from this end, or
        # None for no ceiling. Set by the guest runner, whose peer is a
        # single-threaded supervisor that parses each body whole — and
        # is PID 1 on a VM rung, where exhausting it is a panic rather
        # than a failed exec. Left None on the host side: a body the
        # host composed is not a payload anyone needs protecting from.
        #
        # Binary attachments are deliberately outside it. They are read
        # as opaque bytes rather than parsed, and capping them here
        # would silently make cache writes a wire question when their
        # size is a question about cache semantics (see ROADMAP,
        # "cache-as-service semantics").
        self.send_cap = send_cap

    # ---- framing ----------------------------------------------------

    def _send_msg(
        self, msg: dict, bins: list[bytes], deadline: float | None = None
    ) -> None:
        msg = dict(msg, nbin=len(bins))
        data = json.dumps(msg, separators=(",", ":")).encode()
        if self.send_cap is not None and len(data) > self.send_cap:
            # Checked here, at the one place everything funnels through,
            # because the per-value guards upstream can only bound what
            # they were pointed at. Names, argument counts, and any path
            # nobody thought to guard all arrive here anyway.
            raise FrameTooLarge(
                f"message body is {len(data)} bytes, over this channel's "
                f"{self.send_cap} byte limit; nothing was sent"
            )
        out = bytearray(_LEN.pack(len(data)) + data)
        for b in bins:
            out += _LEN.pack(len(b)) + b
        _arm(self._sock, deadline)
        self._sock.sendall(out)

    def _recv_msg(
        self, deadline: float | None = None
    ) -> tuple[dict, list[bytes]]:
        (n,) = _LEN.unpack(_recv_exact(self._sock, 4, deadline))
        msg = json.loads(_recv_exact(self._sock, n, deadline).decode())
        bins = []
        for _ in range(int(msg.get("nbin", 0))):
            (bn,) = _LEN.unpack(_recv_exact(self._sock, 4, deadline))
            bins.append(_recv_exact(self._sock, bn, deadline))
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
        self, verb: str, body: dict | None = None, bins: list[bytes] | None = None,
        timeout: float | None = None,
    ) -> tuple[dict, list[bytes]]:
        """Send a request; pump incoming requests until our response.

        ``timeout`` bounds how long the *peer* may take, and raises
        :class:`TimeoutError` when it runs out. It is a budget on the
        whole exchange rather than on any one blocking call: a peer that
        sent a byte a moment ago is not thereby making progress, so a
        per-recv timeout would never fire on a wedged guest that keeps
        the channel warm. Omitted (the default, and every guest-side
        caller) leaves the socket blocking exactly as before.

        Time spent inside ``handler`` does not count against it. The
        channel is bidirectional, so this loop *serves* the peer's
        reverse requests (cache, hostcall, emit) while awaiting its own
        response — and a hostcall that legitimately runs longer than the
        budget is our own slowness, not the guest's. Charging it to the
        guest would time out a session whose only fault was calling a
        slow host object.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            self._next_id += 1
            rid = self._next_id
            self._send_msg(
                {"id": rid, "kind": "req", "verb": verb, "body": body or {}},
                bins or [], deadline,
            )
            while True:
                msg, mbins = self._recv_msg(deadline)
                kind = msg.get("kind")
                if kind == "req":
                    served_at = time.monotonic()
                    self._serve_one(msg, mbins)
                    if deadline is not None:
                        deadline += time.monotonic() - served_at
                elif kind == "resp" and msg.get("id") == rid:
                    return msg.get("body", {}), mbins
                elif kind == "err" and msg.get("id") == rid:
                    raise RemoteError(
                        verb, msg.get("message", ""),
                        msg.get("etype", "RemoteError"),
                    )
                else:
                    raise ProtocolError(
                        f"unexpected frame: {kind!r} id={msg.get('id')}"
                    )
        finally:
            if deadline is not None:
                # Hand the socket back blocking. A timeout is per
                # request, and leaving the last one armed would apply an
                # unrelated budget to whatever runs next on this channel
                # — including serve(), which must never time out.
                try:
                    self._sock.settimeout(None)
                except OSError:
                    pass

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
        except (_Shutdown, _Freeze):
            self._send_msg({"id": rid, "kind": "resp", "body": {}}, [])
            raise
        except Exception as e:  # noqa: BLE001 — boundary: report, don't die
            self._send_msg(
                {"id": rid, "kind": "err", "etype": type(e).__name__,
                 "message": str(e)},
                [],
            )

    def serve(self) -> str:
        """Serve incoming requests until shutdown, freeze, or EOF.

        Returns why serving stopped — ``"shutdown"`` (the shutdown verb
        was served), ``"freeze"`` (the freeze verb: the peer intends to
        snapshot us and the caller should redial rather than die), or
        ``"eof"`` (the socket closed under us).
        """
        try:
            while True:
                msg, bins = self._recv_msg()
                if msg.get("kind") != "req":
                    raise ProtocolError(f"server got {msg.get('kind')!r}")
                self._serve_one(msg, bins)
        except _Shutdown:
            return "shutdown"
        except _Freeze:
            return "freeze"
        except ChannelClosed:
            return "eof"

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


class _Shutdown(Exception):
    """Raised by a handler to end ``serve()`` after responding."""


class _Freeze(Exception):
    """Raised by a handler to end ``serve()`` after responding, telling
    the serving loop the peer is about to snapshot the machine — the
    caller should close and redial instead of treating it as an exit."""


def shutdown_served() -> None:
    """Handlers call this on the ``shutdown`` verb (responds, then exits)."""
    raise _Shutdown()


def freeze_served() -> None:
    """Handlers call this on the ``freeze`` verb (responds, then hands
    control back to the redial loop). Distinct from shutdown so that a
    bare EOF keeps meaning "die" — only an explicit, acked freeze puts
    the guest into its redial-and-wait posture."""
    raise _Freeze()
