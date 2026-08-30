"""``dud-emit``: the emit channel, reachable from bash.

DESIGN names this as the forcing function for the whole emit contract:

    the emit channel is specced so bash can use it. Bash has no
    namespace, no objects, no pickle — if the contract is ergonomic
    from bash, it's language-neutral by construction.

Until now that was asserted in the doc and absent from the code, which
made the language-neutrality claim untested — the cheapest possible
proof of "a second runner would speak the same protocol" is a shell
script, and it is free to change the emit contract before a second
runner exists and expensive afterwards.

Deliberately not a second wire verb. A record written here is turned
into the *existing* ``emit`` request by the supervisor, so the host
sees no difference between an emit from Python and one from bash —
which is the point being proved. What bash gets is a way to *reach*
the contract, not a contract of its own.

The transport is a pipe the supervisor already selects on for the
script's output (see :func:`dud.guest.shell.run_shell`), so an emit
arrives live, mid-exec, exactly as the Python runner's does rather
than being collected at the end.

Invoked as ``dud-emit NAME [VALUE]``; the console script in the rootfs
is a two-line shim onto :func:`main`.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys

#: Where ``run_shell`` puts the write end of the emit pipe, and the
#: lock that serializes writers into it.
FD_VAR = "DUD_EMIT_FD"
LOCK_VAR = "DUD_EMIT_LOCK"

#: One record. Generous against anything a shell can put in argv (Linux
#: caps a single argument at 128 KB) and bounded for the same reason
#: every other guest payload is: the supervisor reading this is PID 1
#: on a VM rung.
CAP = 1 << 20


def tag(raw: str | None) -> dict:
    """A shell word as a codec value.

    JSON when it parses, the literal string when it does not. That
    rule is what makes the common cases both work without a flag —
    ``dud-emit rows '{"n": 3}'`` and ``dud-emit status running`` — at
    the cost of one sharp edge worth knowing: ``dud-emit n 42`` emits
    the number 42, not the string "42". Quoting it as ``'"42"'`` is the
    escape hatch, and is itself valid JSON.

    Absent means ``null``, matching ``emit(name)`` in Python.
    """
    if raw is None:
        return {"t": "json", "v": None}
    try:
        return {"t": "json", "v": json.loads(raw)}
    except ValueError:
        return {"t": "json", "v": raw}


def record(name: str, raw: str | None) -> bytes:
    """One newline-delimited JSON frame.

    NDJSON rather than the length-prefixed framing the socket uses,
    because this is the side a shell script has to be able to reason
    about — and a compact JSON object never contains a literal newline
    (the encoder escapes them inside strings), so the delimiter is
    unambiguous without anyone counting bytes.
    """
    body = json.dumps({"name": str(name), "value": tag(raw)},
                      separators=(",", ":"))
    return body.encode() + b"\n"


def send(frame: bytes, fd: int, lock_path: str | None) -> None:
    """Write one frame to the emit pipe, whole.

    A pipe only guarantees atomicity for writes up to ``PIPE_BUF``,
    which is 512 bytes on macOS — small enough that two backgrounded
    ``dud-emit`` calls could interleave and corrupt each other's
    records. Locking rather than capping at 512 keeps the useful sizes
    available, and keeps every rung behaving identically, which capping
    to the platform's own PIPE_BUF would not (Linux's is 4096).

    ``flock`` on a side file rather than on the pipe itself: flock of a
    pipe fd is ENOTSUP on macOS, so the subprocess rung could not have
    used it.
    """
    if lock_path is None:
        os.write(fd, frame)
        return
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            os.write(fd, frame)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or len(argv) > 2 or argv[0].startswith("-"):
        sys.stderr.write(
            "usage: dud-emit NAME [VALUE]\n"
            "  VALUE is JSON if it parses, otherwise a plain string;\n"
            "  omitted means null.\n"
        )
        return 2
    name, raw = argv[0], (argv[1] if len(argv) > 1 else None)

    raw_fd = os.environ.get(FD_VAR)
    if not raw_fd:
        # Outside an exec_shell there is nothing to emit *to*. Said
        # plainly rather than silently succeeding, because an event
        # that went nowhere is indistinguishable from one never fired.
        sys.stderr.write(
            f"dud-emit: {FD_VAR} is not set — emits are only available "
            f"inside a dud shell exec\n"
        )
        return 1
    try:
        fd = int(raw_fd)
    except ValueError:
        sys.stderr.write(f"dud-emit: {FD_VAR}={raw_fd!r} is not an fd\n")
        return 1

    frame = record(name, raw)
    if len(frame) > CAP:
        sys.stderr.write(
            f"dud-emit: record is {len(frame)} bytes, over the {CAP} byte "
            f"limit; write it to a workspace file instead\n"
        )
        return 1
    try:
        send(frame, fd, os.environ.get(LOCK_VAR))
    except OSError as e:
        sys.stderr.write(f"dud-emit: could not write the emit: {e}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
