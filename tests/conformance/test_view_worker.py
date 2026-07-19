"""Conformance: the view worker (VM rung only).

View execs (fs_readonly) fork from a warm import template instead of
paying interpreter spawn + imports per request. The contract pinned
here: the worker path is observable (env marker, ping), routing is
views-only, read-only enforcement and timeouts survive the fork path,
and losing the template degrades to the spawn path, never to an error.
"""

import os
import time

import pytest

_BACKEND = os.environ.get("DUD_BACKEND", "subprocess")

pytestmark = pytest.mark.skipif(
    _BACKEND != "vfkit", reason="the view worker needs the VM rung (fork under PID 1)"
)

_MARKER = "import os\nworker = os.environ.get('DUD_VIEW_WORKER') == '1'"


def _await_worker(session, timeout=60.0):
    deadline = time.monotonic() + timeout
    while True:
        state = session.ping().get("view_worker")
        if state == "ready":
            return
        assert state in ("warming", "ready"), f"view worker is {state!r}"
        assert time.monotonic() < deadline, "worker never warmed"
        time.sleep(0.25)


def test_view_execs_route_through_worker_others_do_not(session):
    _await_worker(session)
    r = session.python(_MARKER, fs_readonly=True)
    assert r.ok and r.outputs["worker"] is True
    r2 = session.python(_MARKER)
    assert r2.ok and r2.outputs["worker"] is False  # spawn path unchanged


def test_worker_execs_keep_readonly_and_freshness(session):
    _await_worker(session)
    r = session.python(
        "try:\n"
        "    open('evil.txt', 'w').write('x')\n"
        "    blocked = False\n"
        "except OSError:\n"
        "    blocked = True\n"
        "leak = 'planted'",
        fs_readonly=True,
    )
    assert r.ok and r.outputs["blocked"] is True
    assert session.diff().empty
    r2 = session.python("fresh = 'leak' not in dir()", fs_readonly=True)
    assert r2.ok and r2.outputs["fresh"] is True


def test_worker_timeout_kills_child_not_worker(session):
    _await_worker(session)
    r = session.python("import time\ntime.sleep(30)", timeout=2.0,
                       fs_readonly=True)
    assert not r.ok and r.error.etype == "Timeout"
    r2 = session.python("y = 2", fs_readonly=True)
    assert r2.ok and r2.outputs["y"] == 2
    assert session.ping().get("view_worker") == "ready"


def test_reset_guest_rewarms_template(session):
    """The pooled park path: the reset kill-sweep takes the template;
    the next session must find a fresh one warming, not a corpse."""
    _await_worker(session)
    session._ch.request("reset_guest", {})
    _await_worker(session)
    r = session.python(_MARKER, fs_readonly=True)
    assert r.ok and r.outputs["worker"] is True


def test_large_session_env_stays_on_worker_path(session):
    """A total env bigger than the guest's socket buffer must cross
    the ctl channel whole (the partial-sendmsg regression). Eight
    60 KB vars: ~480 KB total, each under the per-string execve cap."""
    _await_worker(session)
    r0 = session.shell(
        "for i in 1 2 3 4 5 6 7 8; do "
        "export BLOB$i=$(python3 -c 'print(\"x\" * 60000)'); done && echo set"
    )
    assert "set" in r0.transcript
    code = (_MARKER + "\nimport os\ntotal = sum("
            "len(os.environ.get(f'BLOB{i}', '')) for i in range(1, 9))")
    r = session.python(code, fs_readonly=True)
    assert r.ok and r.outputs["worker"] is True  # warm, no fallback
    assert r.outputs["total"] == 480000  # and the env arrived intact


def test_template_death_degrades_then_rewarms(session):
    _await_worker(session)
    kill = (
        "import os\n"
        "for p in os.listdir('/proc'):\n"
        "    if not p.isdigit():\n"
        "        continue\n"
        "    try:\n"
        "        cmd = open(f'/proc/{p}/cmdline', 'rb').read()\n"
        "    except OSError:\n"
        "        continue\n"
        "    if b'dud.guest.template' in cmd:\n"
        "        os.kill(int(p), 9)\n"
        "done = True"
    )
    r = session.python(kill)  # spawn path: don't route through the victim
    assert r.ok and r.outputs["done"] is True
    # Next view exec works regardless (fallback or fresh template)...
    r2 = session.python("z = 3", fs_readonly=True)
    assert r2.ok and r2.outputs["z"] == 3
    # ...and the worker comes back.
    _await_worker(session)
