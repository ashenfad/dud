"""``dud-hostcall``: synchronous host calls, reachable from bash.

``dud-emit``'s round-trip sibling: where emit is fire-and-forget, this
blocks for the host's answer. Same NDJSON-over-inherited-fd transport
shape, same per-exec scoping (fd numbers and lock paths die with the
exec — see ``_DROP_VARS`` in :mod:`dud.guest.shell`).

Invoked as ``dud-hostcall OBJ METHOD [ARGS...]``; every word after
METHOD crosses as a verbatim string. Deliberately no ``dud-emit``-style
JSON coercion: from a shell, ``commit -m 42`` must stay the string
``"42"`` — silent type changes in arguments the host will act on are
exactly what the hostcall arg guard refuses to do quietly (see
``tests/guest/test_hostcall_args.py``). Structured arguments are a
Python-runner concern.

Protocol: under the request lock — held for the whole round trip, so
frames need no ids and concurrent callers serialize instead of
interleaving — write one frame ``{"obj", "method", "args"}`` to
``DUD_HOSTCALL_REQ``, then read exactly one frame from
``DUD_HOSTCALL_RESP``: ``{"ok": true, "value": tagged}`` or
``{"ok": false, "error": message}``. Every request earns exactly one
response (malformed frames included), so a caller can never block
forever waiting for an answer that is not coming.
"""

from __future__ import annotations

import base64
import binascii
import fcntl
import json
import os
import sys

#: Where ``run_shell`` puts the pipe ends, and the lock serializing
#: whole round trips. Per-exec like the emit channel's: an fd number
#: from one call is meaningless in the next.
REQ_VAR = "DUD_HOSTCALL_REQ"
RESP_VAR = "DUD_HOSTCALL_RESP"
LOCK_VAR = "DUD_HOSTCALL_LOCK"

#: One frame. Same generosity and same reasoning as the emit cap: a
#: shell can legally hold ~128 KB in a single argv word, and the
#: supervisor reading this is PID 1 on a VM rung.
CAP = 1 << 20

_USAGE = (
    "usage: dud-hostcall OBJ METHOD [ARGS...]\n"
    "  ARGS cross as verbatim strings; the host's answer prints to stdout.\n"
)


def _frame(obj: str, method: str, args: list[str]) -> bytes:
    body = json.dumps(
        {
            "obj": obj,
            "method": method,
            "args": [{"t": "json", "v": word} for word in args],
        },
        separators=(",", ":"),
    )
    return body.encode() + b"\n"


def _read_response(fd: int) -> dict:
    """The one response frame; EOF or garbage is an error, never a hang."""
    buf = bytearray()
    while True:
        try:
            chunk = os.read(fd, 65536)
        except OSError as e:
            raise ValueError(f"could not read the host answer: {e}") from e
        if not chunk:
            raise ValueError(
                "the host closed the answer channel without answering"
            )
        buf += chunk
        if b"\n" in buf:
            line, _, _ = bytes(buf).partition(b"\n")
            try:
                body = json.loads(line.decode())
            except (ValueError, UnicodeDecodeError) as e:
                raise ValueError(f"the host answer is not JSON: {e}") from e
            if not isinstance(body, dict):
                raise ValueError("the host answer is not an object")
            return body
        if len(buf) > CAP + (1 << 16):
            raise ValueError("the host answer exceeds the frame cap")


def _print_value(tagged: dict) -> None:
    """The answer on stdout. Strings raw (the producer owns formatting,
    like ``cat``); bytes as raw stdout bytes; anything else compact
    JSON. An untagged value is a host bug, said plainly."""
    if not isinstance(tagged, dict) or "t" not in tagged:
        raise ValueError(f"the host answer value is untagged: {tagged!r}")
    t = tagged.get("t")
    if t == "json":
        value = tagged.get("v")
        if isinstance(value, str):
            sys.stdout.write(value)
        else:
            sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    elif t == "bytes":
        sys.stdout.flush()
        sys.stdout.buffer.write(base64.b64decode(tagged.get("b64", "")))
    else:
        raise ValueError(f"the host answer value has tag {t!r}")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 2 or argv[0].startswith("-"):
        sys.stderr.write(_USAGE)
        return 2
    obj, method, args = argv[0], argv[1], argv[2:]

    raw_req = os.environ.get(REQ_VAR)
    raw_resp = os.environ.get(RESP_VAR)
    lock_path = os.environ.get(LOCK_VAR)
    if not raw_req or not raw_resp or not lock_path:
        # Outside an exec_shell there is nothing to call *through*.
        # Said plainly like dud-emit's equivalent, because a call
        # that went nowhere is indistinguishable from one never made.
        sys.stderr.write(
            f"dud-hostcall: {REQ_VAR} is not set — host calls are only "
            f"available inside a dud shell exec\n"
        )
        return 1
    try:
        req_fd, resp_fd = int(raw_req), int(raw_resp)
    except ValueError:
        sys.stderr.write("dud-hostcall: channel fds are not fds\n")
        return 1

    frame = _frame(obj, method, args)
    if len(frame) > CAP:
        sys.stderr.write(
            f"dud-hostcall: request is {len(frame)} bytes, over the {CAP} "
            f"byte limit; write it to a workspace file instead\n"
        )
        return 1
    try:
        with open(lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                os.write(req_fd, frame)
                answer = _read_response(resp_fd)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
    except (OSError, ValueError) as e:
        sys.stderr.write(f"dud-hostcall: {e}\n")
        return 1
    if not answer.get("ok"):
        sys.stderr.write(f"dud-hostcall: {answer.get('error', 'host failed')}\n")
        return 1
    try:
        _print_value(answer.get("value"))
    except (ValueError, binascii.Error) as e:
        sys.stderr.write(f"dud-hostcall: {e}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
