"""Conformance: the scratch plane (VM rung only — needs block devices).

The contract under test (DESIGN.md "The scratch plane"): /tmp is a
writable ext4 volume whose contents are CACHE — outside the diff,
promoted to the master only on clean park/shutdown, discarded on a
crash. Losing scratch is an inconvenience; trusting it is a bug.
"""

import os
import shutil
import signal

import pytest

_BACKEND = os.environ.get("DUD_BACKEND", "subprocess")

pytestmark = pytest.mark.skipif(
    _BACKEND != "vfkit", reason="scratch volumes need the VM rung"
)


@pytest.fixture(scope="module")
def blank():
    """The cached blank master (bakes once ever per size, via host
    mke2fs or the builder VM)."""
    from dud.images.scratch import blank_ext4

    return blank_ext4(512)


def _boot(scratch):
    from dud.backends.vfkit import VfkitSession

    return VfkitSession(
        medium=os.environ.get("DUD_MEDIUM", "initramfs"), scratch=scratch
    )


def test_scratch_mounts_persists_and_discards_on_crash(blank, tmp_path):
    master = tmp_path / "master.ext4"
    shutil.copyfile(blank, master)  # test-local master

    # Boot 1: /tmp is the ext4 volume; writes there are not state.
    s = _boot(master)
    try:
        r = s.shell("df -T /tmp | tail -1 && echo warm > /tmp/cache.txt")
        assert "ext4" in r.transcript
        assert s.diff().empty  # scratch never enters the diff
    finally:
        s.close()  # graceful shutdown -> promotion

    # Boot 2: the promoted cache is there. Then crash with an
    # unpromoted write in flight.
    s2 = _boot(master)
    r2 = s2.shell("cat /tmp/cache.txt && echo ghost > /tmp/lost.txt && sync")
    assert "warm" in r2.transcript
    os.kill(s2._proc.pid, signal.SIGKILL)
    try:
        s2.close()
    except Exception:
        pass

    # Boot 3: clean-park survivors only — the crashed VM's write was
    # never promoted, even though the guest had synced it.
    s3 = _boot(master)
    try:
        r3 = s3.shell("cat /tmp/cache.txt; ls /tmp")
        assert "warm" in r3.transcript
        assert "lost.txt" not in r3.transcript
    finally:
        s3.close()
