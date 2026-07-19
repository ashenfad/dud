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
