"""The view-worker template: fork-per-request runners, imports warm.

Every exec pays a fixed tax before user code runs: interpreter spawn
plus importing its libraries (~0.4 s for the DS stack even with baked
.pyc). This process amortizes that tax for *view* execs (read-only
app handlers): it imports the image's installed packages once, then
parks; each request forks it — CoW memory keeps the imports warm, and
fork gives a genuinely fresh namespace because the template never
runs user code. Children execute one request via the ordinary runner
and exit, so nothing a request does can reach the template or the
next request. sandtrap served views from a warm process too — this is
parity, not a cheat.

Control protocol (over the socketpair the supervisor holds):
  - template -> supervisor: one ``b"R"`` byte when warm (ready).
  - supervisor -> template, per request: a 4-byte BE length + JSON
    ``{"cwd": ..., "env": {...}}`` frame with the exec socket riding
    ancillary as SCM_RIGHTS.
  - template -> supervisor: the child's pid as 8 bytes BE (the
    supervisor kills that process group on timeout).

The child marks itself with ``DUD_VIEW_WORKER=1`` in the exec's env —
observable by guest code and pinned by conformance (no silent
fallback). ``DUD_TEMPLATE_WARM=0`` skips the import warm-up (tests).

Invoked as: python -m dud.guest.template <ctl-fd>
"""

from __future__ import annotations

import array
import json
import os
import signal
import socket
import struct
import sys

_LEN = struct.Struct(">I")


def _warm() -> None:
    """Import every installed distribution's top-level modules,
    best-effort. Failures (missing extras, platform guards, import
    side effects) are skipped — warmth is an optimization, never a
    correctness dependency."""
    import importlib.metadata as md

    names: set[str] = set()
    try:
        dists = list(md.distributions())
    except Exception:
        return
    for dist in dists:
        try:
            top = dist.read_text("top_level.txt")
        except Exception:
            top = None
        if top:
            names.update(line.strip() for line in top.splitlines())
    for name in sorted(names):
        if not name or name.startswith("_") or name == "dud":
            continue
        try:
            __import__(name)
        except BaseException:  # noqa: BLE001 — warmth only, never fatal
            pass


def _reap() -> None:
    try:
        while os.waitpid(-1, os.WNOHANG) != (0, 0):
            pass
    except ChildProcessError:
        pass


def _recv_request(ctl: socket.socket) -> tuple[dict, int] | None:
    """One control frame: (request, exec-socket fd). None on EOF."""
    fds = array.array("i")
    try:
        msg, ancdata, _flags, _addr = ctl.recvmsg(4, socket.CMSG_LEN(4))
    except OSError:
        return None
    if not msg:
        return None
    for level, ctype, data in ancdata:
        if level == socket.SOL_SOCKET and ctype == socket.SCM_RIGHTS:
            fds.frombytes(data[: len(data) - (len(data) % 4)])
    (n,) = _LEN.unpack(msg)
    buf = bytearray()
    while len(buf) < n:
        chunk = ctl.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    if not fds:
        return None
    return json.loads(bytes(buf).decode()), fds[0]


def main() -> None:
    ctl = socket.socket(fileno=int(sys.argv[1]))
    # Reap children the moment they exit (PEP 475 retries the blocked
    # recv around the handler): a finished runner must vanish from the
    # process table promptly, because zombies still answer signal-0 and
    # would stall the supervisor's liveness checks.
    signal.signal(signal.SIGCHLD, lambda *_: _reap())
    if os.environ.get("DUD_TEMPLATE_WARM", "1") != "0":
        _warm()
    ctl.sendall(b"R")
    while True:
        _reap()
        got = _recv_request(ctl)
        if got is None:
            return  # supervisor went away (or a torn frame): power down
        req, exec_fd = got
        pid = os.fork()
        if pid == 0:
            # Child: become one ordinary runner serving one request.
            os.setsid()  # its own group: the supervisor killpg's on timeout
            try:
                # The template's auto-reaper must not leak into user
                # code (it would steal subprocess.wait()'s statuses).
                signal.signal(signal.SIGCHLD, signal.SIG_DFL)
                ctl.close()
                os.environ.clear()
                os.environ.update(req.get("env") or {})
                os.environ["DUD_VIEW_WORKER"] = "1"
                try:
                    os.chdir(req.get("cwd") or "/")
                except OSError:
                    pass
                from .runner import serve

                serve(socket.socket(fileno=exec_fd))
            except BaseException:  # noqa: BLE001 — child never re-enters the loop
                os._exit(1)
            os._exit(0)
        os.close(exec_fd)
        ctl.sendall(pid.to_bytes(8, "big"))


if __name__ == "__main__":
    main()
