"""Conformance: freeze/thaw (firecracker snapshots).

The contract under test: freeze() parks a running VM as files (zero
RAM, zero CPU, no VMM process) and thaw() resumes it with ALL guest
state — filesystem, shell env, live memory — exactly where it was.
The guest cooperates via the freeze verb, so a bare EOF still means
"die"; the wall clock is resynced on thaw; and a frozen bundle
discarded without a thaw is a disposal path (no scratch promotion).

Firecracker only: vfkit has no snapshot facility (that rung parks hot).
"""

import os
import time

import pytest

_BACKEND = os.environ.get("DUD_BACKEND", "subprocess")

pytestmark = pytest.mark.skipif(
    _BACKEND != "firecracker",
    reason="freeze/thaw is snapshot-based parking (firecracker only)",
)


def _boot(**kwargs):
    import dud

    kwargs.setdefault("memory_mib", 1024)  # see conftest._TEST_VM_MIB
    return dud.session(
        _BACKEND, medium=os.environ.get("DUD_MEDIUM", "initramfs"), **kwargs
    )


def test_freeze_thaw_preserves_guest_state():
    s = _boot()
    try:
        s.shell("echo alive > /workspace/note.txt && export FROZEN_VAR=carried")
        s.freeze()

        # Frozen: no VMM process, snapshot files exist, marker owns it.
        assert s.frozen
        assert s._proc.poll() is not None
        for name in ("vmstate", "mem"):
            assert os.path.getsize(os.path.join(s._rundir, name)) > 0
        assert open(os.path.join(s._rundir, "frozen")).read() == str(os.getpid())

        t0 = time.monotonic()
        s.thaw()
        thaw_ms = (time.monotonic() - t0) * 1000

        assert not s.frozen
        assert not os.path.exists(os.path.join(s._rundir, "frozen"))
        r = s.shell("cat /workspace/note.txt && echo var=$FROZEN_VAR")
        assert "alive" in r.transcript
        assert "var=carried" in r.transcript
        print(f"thaw: {thaw_ms:.0f}ms")
    finally:
        s.close()


def test_thaw_preserves_live_memory_and_resyncs_clock():
    """The differentiator over a reboot: memory survives. A guest
    process started pre-freeze is still running post-thaw, and the
    wall clock (stopped at snapshot time) is resynced."""
    s = _boot()
    try:
        s.shell("sleep 300 & echo pid=$! > /workspace/sleeper")
        s.freeze()
        time.sleep(3.0)  # let the wall clock fall behind while frozen
        s.thaw()
        r = s.shell("kill -0 $(cut -d= -f2 /workspace/sleeper) && echo survivor")
        assert "survivor" in r.transcript
        # Tolerance below the frozen duration: if resync were broken,
        # the guest clock would trail by the full 3s and fail this.
        out = s.python("import time\nnow = time.time()")
        assert abs(out.outputs["now"] - time.time()) < 2.0
    finally:
        s.close()


def test_repeated_freeze_thaw_cycles():
    s = _boot()
    try:
        for i in range(3):
            s.shell(f"echo {i} >> /workspace/cycles")
            s.freeze()
            s.thaw()
        r = s.shell("cat /workspace/cycles")
        assert r.transcript.split() == ["0", "1", "2"]
    finally:
        s.close()


def test_view_worker_rewarms_after_thaw():
    """Clones of one snapshot would share the template's PRNG state;
    resync replaces it, and it comes back ready."""
    s = _boot()
    try:
        deadline = time.monotonic() + 30
        while s.ping()["view_worker"] != "ready":
            assert time.monotonic() < deadline, "template never warmed"
            time.sleep(0.25)
        s.freeze()
        s.thaw()
        deadline = time.monotonic() + 30
        while s.ping()["view_worker"] != "ready":
            assert time.monotonic() < deadline, "template never re-warmed"
            time.sleep(0.25)
    finally:
        s.close()


def test_discard_while_frozen_is_disposal():
    """close() on a frozen session removes the bundle without a thaw
    and never promotes scratch (disposal paths never promote)."""
    import shutil

    from dud.images.scratch import blank_ext4

    s = _boot()
    rundir = s._rundir
    s.freeze()
    s.close()
    assert not os.path.exists(rundir)

    # And with a scratch volume: the master must be untouched.
    import tempfile

    with tempfile.TemporaryDirectory(dir="/tmp") as td:
        master = os.path.join(td, "master.ext4")
        shutil.copyfile(blank_ext4(256), master)
        before = os.path.getmtime(master)
        s2 = _boot(scratch=master)
        s2.shell("echo tainted > /tmp/poison && sync")
        s2.freeze()
        s2.close()
        assert os.path.getmtime(master) == before
