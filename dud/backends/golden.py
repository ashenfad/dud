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
import json
import logging
import shutil
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..atomic import staged
from ..errors import DudError
from ..images import dud_home
from ..images.builder import PIPELINE_VERSION, _dud_code_hash

_log = logging.getLogger(__name__)

#: The two files firecracker needs to restore. The rootfs is NOT among
#: them: a snapshot references it by the path recorded at freeze time,
#: which is safe only because image artifacts are content-addressed and
#: outlive any one session.
_PARTS = ("vmstate", "mem")

#: What the snapshot was booted from, recorded beside it. See `verify`.
_MANIFEST = "manifest.json"

#: Bump when the golden layout or restore contract changes shape, so a
#: new dud cannot find an old dud's snapshots at all.
_SNAPSHOT_VERSION = 1


class StaleGolden(DudError):
    """A snapshot exists for this key but was booted from other bits."""


@lru_cache(maxsize=1)
def _code_identity() -> str:
    """This dud's guest-code identity: pipeline version + code hash.

    Cached because it reads every injected source file and
    ``golden_dir`` is on the acquire path. Safe for a process lifetime —
    dud's own code does not change underneath a running host.
    """
    return f"p{PIPELINE_VERSION}:{_dud_code_hash()}"


def golden_dir(fingerprint: str, home: Path | None = None) -> Path:
    """Where the snapshot for this boot fingerprint lives.

    The pool's fingerprint alone is NOT enough of a key here, and the
    difference matters because this path outlives the process. That
    fingerprint serializes raw constructor kwargs — ``image`` is a tag,
    not a digest, and dud's own guest code appears nowhere in it. The
    pool's in-memory buckets can live with that, since they die before
    any of it can drift. A directory under ``~/.dud`` cannot.

    Left unqualified, upgrading dud would keep every snapshot: same
    kwargs, same key, and a restore that resumes a guest booted from
    the PREVIOUS release's rootfs. ``proto.py`` documents host/guest
    skew as having "no path through the normal build" because the
    rootfs is content-addressed over ``_dud_code_hash``; it also names
    the exception, "a future rung that rents a machine it did not
    build," which is exactly what a restore is. PROTO_VERSION would not
    catch it either — that moves when the framing changes, not when
    guest logic does.

    So the guest-code identity is folded in here, which is cheap and
    purely local. What it cannot see — a re-pushed image tag resolving
    to new bytes — is caught by `verify` instead.
    """
    key = "\0".join((f"snap{_SNAPSHOT_VERSION}", _code_identity(), fingerprint))
    digest = hashlib.sha256(key.encode()).hexdigest()[:24]
    return (home or dud_home()) / "golden" / digest


def usable(path: Path) -> bool:
    """Is there a complete snapshot here?

    Every part, or none: a half-written set is worse than an absent
    one, because it would be found and then fail at restore. Creation
    publishes atomically, so this only guards against a directory left
    by an older, interrupted dud — including one that predates the
    manifest, which is why the manifest counts as a part.
    """
    return all((path / name).is_file() for name in (*_PARTS, _MANIFEST))


def eligible(kwargs: dict[str, Any]) -> bool:
    """May this config use golden snapshots at all?

    A snapshot records the absolute path of every backing file the VM
    had, and can only be restored where all of them still exist. The
    rootfs qualifies — content-addressed, shared, outlives any session.
    Caller-supplied ``disks`` qualify: their paths are the caller's.

    ``scratch`` does not. It is cloned per boot into the session's own
    rundir, so the seed's ``scratch.img`` is recorded into ``vmstate``
    and then deleted with the rundir the moment seeding finishes. Every
    later restore would reference a file that is gone — and because a
    failed restore discards the snapshot and reseeds, that is not one
    slow boot but a permanent loop: a failed restore, a cold boot, and
    a background boot-plus-freeze on every single miss, forever.
    """
    return not kwargs.get("scratch")


def _identity(rootfs: Path, kernel: Path) -> dict[str, str]:
    return {"code": _code_identity(),
            "rootfs": str(rootfs), "kernel": str(kernel)}


def verify(path: Path, rootfs: Path, kernel: Path) -> None:
    """Refuse a snapshot booted from bits other than the ones resolved.

    The last line against staleness that the key cannot draw. An image
    TAG can resolve to new bytes without any kwarg changing, and a
    bundled kernel can move the same way; both land on an unchanged
    fingerprint. The resolved rootfs path is content-addressed over the
    image digest, pipeline version and guest code, so comparing it is a
    complete check — and it costs nothing, because the session resolves
    its build before it looks at ``restore_from`` anyway.
    """
    want = _identity(rootfs, kernel)
    try:
        got = json.loads((path / _MANIFEST).read_text())
    except (OSError, ValueError) as e:
        raise StaleGolden(f"unreadable golden manifest at {path}: {e}") from e
    if got != want:
        diff = [k for k in want if got.get(k) != want[k]]
        raise StaleGolden(
            f"golden snapshot at {path} was booted from different "
            f"{', '.join(diff)}; discarding it and booting fresh"
        )


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
            # Written last, inside the same staging dir, so `usable`
            # can treat its presence as "this snapshot is complete AND
            # says what it was booted from".
            (tmp / _MANIFEST).write_text(json.dumps(
                _identity(session.build.rootfs_path, session._kernel_path),
                sort_keys=True))
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
