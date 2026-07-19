"""View-worker template control protocol, driven from the host.

fork + SCM_RIGHTS work on macOS too, so the fork-per-request contract
is testable without a VM. DUD_TEMPLATE_WARM=0 skips the import sweep
(the sweep is warmth, not correctness).
"""

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from dud.proto import Channel
from dud.values import decode_map

REPO = Path(__file__).resolve().parents[2]


def _recvn(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("template EOF")
        buf += chunk
    return buf


@pytest.fixture
def template():
    ours, theirs = socket.socketpair()
    proc = subprocess.Popen(
        [sys.executable, "-m", "dud.guest.template", str(theirs.fileno())],
        pass_fds=(theirs.fileno(),),
        env={"DUD_TEMPLATE_WARM": "0", "PATH": os.environ.get("PATH", ""),
             "PYTHONPATH": str(REPO)},
        cwd=str(REPO),
    )
    theirs.close()
    ours.settimeout(15.0)
    assert ours.recv(1) == b"R"  # ready byte after (skipped) warm-up
    yield ours
    ours.close()
    proc.kill()
    proc.wait(timeout=5)


def _fork(ctl: socket.socket, cwd: str, env: dict) -> tuple[socket.socket, int]:
    parent, child = socket.socketpair()
    payload = json.dumps({"cwd": cwd, "env": env}).encode()
    ctl.sendmsg(
        [struct.pack(">I", len(payload)) + payload],
        [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
          struct.pack("i", child.fileno()))],
    )
    child.close()
    pid = int.from_bytes(_recvn(ctl, 8), "big")
    return parent, pid


def _run(parent: socket.socket, code: str) -> dict:
    parent.settimeout(15.0)
    ch = Channel(parent)
    ch._send_msg({"id": 1, "kind": "req", "verb": "run",
                  "body": {"code": code}}, [])
    msg, _ = ch._recv_msg()
    assert msg["kind"] == "resp", msg
    return msg["body"]


def test_forked_child_serves_run_with_env_cwd_and_marker(template, tmp_path):
    parent, pid = _fork(template, str(tmp_path),
                        {"HOME": "/", "STAMP": "s1"})
    assert pid > 0
    body = _run(parent, (
        "import os\n"
        "marker = os.environ.get('DUD_VIEW_WORKER')\n"
        "stamp = os.environ.get('STAMP')\n"
        "where = os.path.realpath(os.getcwd())\n"
    ))
    assert body["ok"], body
    outs = decode_map(body["outputs"])
    assert outs["marker"] == "1"  # the worker path is observable
    assert outs["stamp"] == "s1"  # per-request env applied
    assert outs["where"] == str(tmp_path.resolve())
    parent.close()


def test_children_cannot_pollute_template_or_each_other(template, tmp_path):
    p1, _ = _fork(template, str(tmp_path), {})
    b1 = _run(p1, "import sys\nsys.PWNED = True\nset_it = True")
    assert b1["ok"], b1
    p1.close()
    p2, _ = _fork(template, str(tmp_path), {})
    b2 = _run(p2, "import sys\nclean = not hasattr(sys, 'PWNED')")
    assert b2["ok"], b2
    assert decode_map(b2["outputs"])["clean"] is True
    p2.close()


def test_children_are_distinct_processes(template, tmp_path):
    p1, pid1 = _fork(template, str(tmp_path), {})
    p2, pid2 = _fork(template, str(tmp_path), {})
    assert pid1 != pid2
    assert decode_map(_run(p1, "import os\nme = os.getpid()")["outputs"])["me"] == pid1
    assert decode_map(_run(p2, "import os\nme = os.getpid()")["outputs"])["me"] == pid2
    p1.close()
    p2.close()
