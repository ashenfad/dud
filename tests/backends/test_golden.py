"""Golden snapshots: the store, and the pool's use of it.

A pool miss used to cold-boot. The boot is a pure function of the boot
fingerprint, so it is done once and cloned after — measured on amd64
CI at 32-52 ms against 1276 ms cold.

These pin the parts that are decisions rather than mechanics: what the
snapshot is keyed by, that a half-written one is never used, that
seeding rides the freeze `release` already does, and that every
failure falls back to booting rather than to no session.
"""

from __future__ import annotations

from pathlib import Path

from dud.backends import golden


def _complete(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for name in ("vmstate", "mem"):
        (path / name).write_bytes(b"x")
    return path


def test_keyed_by_the_boot_fingerprint(tmp_path):
    """Same fingerprint, same snapshot; different fingerprint, a
    different path — so a changed image lands elsewhere instead of
    needing invalidation."""
    a = golden.golden_dir('{"image": "python:3.12-slim"}', home=tmp_path)
    b = golden.golden_dir('{"image": "python:3.12-slim"}', home=tmp_path)
    c = golden.golden_dir('{"image": "node:22-slim"}', home=tmp_path)
    assert a == b and a != c
    assert tmp_path in a.parents


def test_a_half_written_snapshot_is_not_usable(tmp_path):
    """Worse than an absent one: it would be found, then fail at
    restore, on a path whose whole job is to be faster than booting."""
    d = tmp_path / "g"
    d.mkdir()
    assert not golden.usable(d)
    (d / "vmstate").write_bytes(b"x")
    assert not golden.usable(d)
    (d / "mem").write_bytes(b"x")
    assert golden.usable(d)


class _FrozenSession:
    def __init__(self, rundir: Path):
        self._rundir = str(rundir)


def test_publish_copies_both_parts(tmp_path):
    src = _complete(tmp_path / "rundir")
    dest = tmp_path / "golden"
    assert golden.publish_frozen(_FrozenSession(src), dest) is True
    assert golden.usable(dest)
    # Copied, not moved: the session still owns its rundir and may be
    # thawed back out of it.
    assert golden.usable(src)


def test_publish_refuses_when_the_freeze_left_nothing(tmp_path):
    src = tmp_path / "rundir"
    src.mkdir()
    dest = tmp_path / "golden"
    assert golden.publish_frozen(_FrozenSession(src), dest) is False
    assert not dest.exists()


def test_publish_is_atomic(tmp_path):
    """A concurrent creator must never observe a partial snapshot, so
    publication renames a finished directory into place."""
    src = _complete(tmp_path / "rundir")
    dest = tmp_path / "golden"
    golden.publish_frozen(_FrozenSession(src), dest)
    assert sorted(p.name for p in dest.iterdir()) == ["mem", "vmstate"]
    assert not any(p.name.startswith(".") for p in dest.parent.iterdir()
                   if p != dest)


def test_discard_removes_it(tmp_path):
    key = '{"image": "x"}'
    _complete(golden.golden_dir(key, home=tmp_path))
    golden.discard(key, home=tmp_path)
    assert not golden.usable(golden.golden_dir(key, home=tmp_path))
