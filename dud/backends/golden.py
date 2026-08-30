"""Golden snapshots: boot a machine once, then clone it forever.

A pool miss used to mean a cold boot. On firecracker that is ~1.3 s,
and it is the same 1.3 s every time — booting an identical kernel to
run an identical `/init` against an identical rootfs, to arrive at a
state that is a pure function of the boot fingerprint. Nothing about
it is per-session.

So it is done once. The first miss for a fingerprint boots a machine,
quiesces it, freezes it, and keeps the snapshot; every miss after that
restores a *clone* of it. Measured on amd64 CI: **32-52 ms to a
serving VM, against 1276 ms cold.**

This is orthogonal to parking, and the two solve different costs:

    affinity park  skips push_tree  (the tree is already on the VM)
    golden clone   skips the boot   (the machine is already booted)

Parking keeps a *specific* VM alive because its workspace is worth
something. A golden snapshot keeps no VM at all — it is a file that
any number of sessions can start from concurrently, which is why it
can also be cheaper than parking: the ~3 s freeze is paid once per
fingerprint rather than on every release.

Cloning is safe because of two firecracker properties, both verified
rather than assumed (see dev/goldenspike.py):

- the memory file maps ``MAP_PRIVATE``, so clones get copy-on-write
  over the same bytes and none can write through — the golden file
  hashes identically after N concurrent clones;
- ``vsock_override`` re-points each clone's socket, which is the
  documented mechanism for restoring one snapshot into several VMs.

And randomness does not collide across clones (``os.urandom``,
``uuid4`` and ``random`` all measured distinct), because virtio-rng
reseeds the kernel pool on resume and every exec spawns a fresh
interpreter.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from pathlib import Path
from typing import Any

from ..atomic import staged
from ..images import dud_home

_log = logging.getLogger(__name__)

#: The two files firecracker needs to restore. The rootfs is NOT among
#: them: a snapshot references it by the path recorded at freeze time,
#: which is safe only because image artifacts are content-addressed and
#: outlive any one session.
_PARTS = ("vmstate", "mem")


def golden_dir(fingerprint: str, home: Path | None = None) -> Path:
    """Where the snapshot for this boot fingerprint lives.

    Keyed by the fingerprint the pool already computes, hashed only to
    get a filesystem-safe name. That key covers image, packages,
    kernel, memory and medium — everything a booted machine's state is
    a function of — so a changed image simply lands on a different
    path rather than needing invalidation.
    """
    digest = hashlib.sha256(fingerprint.encode()).hexdigest()[:24]
    return (home or dud_home()) / "golden" / digest


def usable(path: Path) -> bool:
    """Is there a complete snapshot here?

    Both parts, or none: a half-written pair is worse than an absent
    one, because it would be found and then fail at restore. Creation
    publishes atomically, so this only guards against a directory left
    by an older, interrupted dud.
    """
    return all((path / name).is_file() for name in _PARTS)


def publish_frozen(session: Any, dest: Path) -> bool:
    """Keep an ALREADY-frozen session's snapshot as the golden.

    Called from ``VmPool.release``, where the freeze has just happened
    and the guest has just been reset — so a template costs one file
    copy rather than a boot plus a freeze, and no caller ever waits for
    it to be made.

    Published through a staging directory so a concurrent creator
    cannot observe a half-written snapshot. Losing that race is fine
    and costs only the duplicated work: whoever renames last wins, and
    both snapshots are equally valid, being functions of the same
    fingerprint.
    """
    src = Path(session._rundir)
    if not all((src / name).is_file() for name in _PARTS):
        _log.warning("freeze produced no snapshot; skipping golden publish")
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with staged(dest, tag="golden") as tmp:
            tmp.mkdir(parents=True, exist_ok=True)
            for name in _PARTS:
                # Copy rather than move: the session still owns its
                # rundir and may be thawed back from it.
                shutil.copyfile(src / name, tmp / name)
    except OSError as e:
        _log.info("could not publish a golden snapshot (%s); "
                  "misses will cold-boot", e)
        return False
    _log.info("published golden snapshot at %s", dest)
    return True


def discard(fingerprint: str, home: Path | None = None) -> None:
    """Drop a golden snapshot. Only for a restore that failed: the
    files are a cache, and a cache that cannot be restored from is
    worse than none."""
    shutil.rmtree(golden_dir(fingerprint, home), ignore_errors=True)
