"""Conformance: pooled VM lifecycle — death is loud, recovery is cheap,
parking posture is invisible.

The disposable thesis as a contract: any VM may vanish at any moment
(crash, pool reclaim under max_total) and the owner's recovery is
always the same move — re-acquire, re-push, retry. And the pool's two
parking postures (vfkit parks hot, firecracker parks frozen) must be
indistinguishable above the acquire/release seam: same reuse, same
state affinity, same hygiene. These tests kill real VMM processes, so
they are VM-rung only.
"""

import os
import signal

import pytest

_BACKEND = os.environ.get("DUD_BACKEND", "subprocess")

pytestmark = pytest.mark.skipif(
    _BACKEND not in ("vfkit", "firecracker"),
    reason="exercises VmPool over real VMMs (VM rungs only)",
)


def _vm_kwargs():
    return {"medium": os.environ.get("DUD_MEDIUM", "initramfs"),
            "memory_mib": 1024}


def _pool():
    from dud.backends.pool import VmPool

    if _BACKEND == "firecracker":
        from dud.backends.firecracker import FirecrackerSession

        return VmPool(session_cls=FirecrackerSession)
    return VmPool()


def test_dead_vm_raises_session_lost_and_reacquire_recovers():
    from dud.backends.base import SessionLost

    pool = _pool()
    try:
        s = pool.acquire(**_vm_kwargs())
        assert s.shell("echo up").transcript.strip() == "up"
        os.kill(s._proc.pid, signal.SIGKILL)
        with pytest.raises(SessionLost):
            s.shell("echo down")
        # The owner's recovery move: close (pool tears the corpse down
        # instead of parking it) and acquire again.
        s.close()
        s2 = pool.acquire(**_vm_kwargs())
        assert s2 is not s
        assert s2.shell("echo back").transcript.strip() == "back"
        s2.close()
    finally:
        pool.close()


def test_pooled_reuse_with_hygiene():
    """close() parks (hot or frozen per rung); the next acquire gets
    the same machine, reset: env gone, workspace empty, imports warm."""
    pool = _pool()
    try:
        s = pool.acquire(**_vm_kwargs())
        s.shell("export LEAK=no && echo residue > /workspace/f.txt")
        marker = s._rundir
        s.close()
        if _BACKEND == "firecracker":
            # Frozen posture: parked = files, no VMM process.
            assert s.frozen and s._proc.poll() is not None
        s2 = pool.acquire(**_vm_kwargs())
        assert s2._rundir == marker  # same machine...
        r = s2.shell("echo ${LEAK:-clean}; ls /workspace")
        assert "clean" in r.transcript  # ...new session
        assert "residue" not in r.transcript
        s2.close()
    finally:
        pool.close()


def test_reuse_is_cheaper_than_booting():
    """A pool that costs more than a boot is not a pool.

    Reuse used to be *slower* than starting fresh: the reset sweep
    counted kernel threads, which cannot be killed, so "only PID 1
    remains" never became true and the sweep burned its whole 2 s
    budget every time — against a 0.94 s cold boot on vfkit. Pooling
    was a pessimization on that rung and nobody noticed, because
    nothing compared the two.

    Timed rather than asserted structurally, because the property IS
    the time. The bound is deliberately loose — measured reuse is
    ~0.02 s and the broken version was a hard 2.0 s floor — so this
    fails the bug without being a benchmark that flakes on a loaded
    runner.

    Timed across close-then-acquire, not acquire alone: the reset runs
    in ``release()``, so the cost lands when a session is handed BACK.
    A first cut of this timed only the acquire, passed happily with the
    bug reintroduced, and measured nothing.
    """
    import time

    pool = _pool()
    try:
        started = time.monotonic()
        s = pool.acquire(**_vm_kwargs())
        boot = time.monotonic() - started

        started = time.monotonic()
        s.close()  # park: this is where reset_guest runs
        s2 = pool.acquire(**_vm_kwargs())
        reuse = time.monotonic() - started
        s2.close()

        assert reuse < 1.5, f"reuse took {reuse:.2f}s (boot was {boot:.2f}s)"
    finally:
        pool.close()


def test_state_affinity_across_park():
    """park_state tags the parked tree; a matching acquire resumes
    without a push — through a freeze/thaw on the frozen posture."""
    pool = _pool()
    try:
        s = pool.acquire(**_vm_kwargs())
        s.shell("echo precious > /workspace/state.txt")
        s.close(park_state="commit-abc")
        hit = pool.acquire(state="commit-abc", **_vm_kwargs())
        assert hit.resumed
        assert "precious" in hit.shell("cat /workspace/state.txt").transcript
        hit.close()
        miss = pool.acquire(state="commit-zzz", **_vm_kwargs())
        assert not miss.resumed  # tag never matches twice / wrong tag
        miss.close()
    finally:
        pool.close()
