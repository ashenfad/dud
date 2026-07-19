"""Conformance: VM death is loud (SessionLost) and recovery is cheap.

The disposable thesis as a contract: any VM may vanish at any moment
(crash, pool reclaim under max_total) and the owner's recovery is
always the same move — re-acquire, re-push, retry. These tests kill
the actual vfkit process, so they are VM-rung only.
"""

import os
import signal

import pytest

_BACKEND = os.environ.get("DUD_BACKEND", "subprocess")

pytestmark = pytest.mark.skipif(
    _BACKEND != "vfkit",
    reason="exercises VmPool, which is vfkit-typed until the snapshot work",
)


def _vm_kwargs():
    return {"medium": os.environ.get("DUD_MEDIUM", "initramfs")}


def test_dead_vm_raises_session_lost_and_reacquire_recovers():
    from dud.backends.base import SessionLost
    from dud.backends.pool import VmPool

    pool = VmPool()
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
