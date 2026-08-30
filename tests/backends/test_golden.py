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

import types
from pathlib import Path

import pytest

from dud.backends import golden


#: What the fake session below claims to have booted from.
_ROOTFS = Path("/artifacts/rootfs-aaaaaaaa")
_KERNEL = Path("/kernels/amd64/Image")


def _frozen_rundir(path: Path) -> Path:
    """What a freeze leaves behind: the parts, and no manifest."""
    path.mkdir(parents=True, exist_ok=True)
    for name in ("vmstate", "mem"):
        (path / name).write_bytes(b"x")
    return path


def _published(path: Path) -> Path:
    """A complete golden: the parts plus the manifest saying what they
    were booted from. Published from a separate rundir, since
    publication renames a staged dir over its destination."""
    src = _frozen_rundir(path.parent / (path.name + "-src"))
    golden.publish_frozen(_FrozenSession(src), path)
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
    # Still not usable: without a manifest there is no way to tell what
    # it was booted from, which is exactly the snapshot a newer dud must
    # not resume. Pre-manifest directories therefore read as incomplete.
    assert not golden.usable(d)
    done = tmp_path / "published"
    golden.publish_frozen(_FrozenSession(d), done)
    assert golden.usable(done)


class _FrozenSession:
    def __init__(self, rundir: Path):
        self._rundir = str(rundir)
        self.build = types.SimpleNamespace(rootfs_path=_ROOTFS)
        self._kernel_path = _KERNEL


def test_publish_copies_both_parts(tmp_path):
    src = _frozen_rundir(tmp_path / "rundir")
    dest = tmp_path / "golden"
    assert golden.publish_frozen(_FrozenSession(src), dest) is True
    assert golden.usable(dest)
    # Copied, not moved: the session still owns its rundir and may be
    # thawed back out of it.
    assert all((src / n).is_file() for n in ("vmstate", "mem"))


def test_publish_refuses_when_the_freeze_left_nothing(tmp_path):
    src = tmp_path / "rundir"
    src.mkdir()
    dest = tmp_path / "golden"
    assert golden.publish_frozen(_FrozenSession(src), dest) is False
    assert not dest.exists()


def test_publish_is_atomic(tmp_path):
    """A concurrent creator must never observe a partial snapshot, so
    publication renames a finished directory into place."""
    src = _frozen_rundir(tmp_path / "rundir")
    dest = tmp_path / "golden"
    golden.publish_frozen(_FrozenSession(src), dest)
    assert sorted(p.name for p in dest.iterdir()) == [
        "manifest.json", "mem", "vmstate"]
    assert not any(p.name.startswith(".") for p in dest.parent.iterdir()
                   if p != dest)


def test_discard_removes_it(tmp_path):
    key = '{"image": "x"}'
    _published(golden.golden_dir(key, home=tmp_path))
    assert golden.usable(golden.golden_dir(key, home=tmp_path))
    golden.discard(key, home=tmp_path)
    assert not golden.usable(golden.golden_dir(key, home=tmp_path))


def test_upgrading_dud_lands_on_a_different_snapshot(monkeypatch, tmp_path):
    """The staleness the pool's fingerprint cannot see.

    That fingerprint serializes raw constructor kwargs, so an upgraded
    dud asking for the same config produces the same key — and would
    find, and resume, a guest booted from the previous release's
    rootfs. `proto.py` calls host/guest skew impossible "through the
    normal build" precisely because the rootfs is content-addressed
    over the guest code; a restore is the exception it names, a machine
    dud did not build. PROTO_VERSION does not catch it either: that
    moves when the framing changes, not when guest logic does.
    """
    key = '{"image": "python:3.12-slim"}'
    golden._code_identity.cache_clear()
    before = golden.golden_dir(key, home=tmp_path)

    monkeypatch.setattr(golden, "_dud_code_hash", lambda: "a-later-release")
    golden._code_identity.cache_clear()
    after = golden.golden_dir(key, home=tmp_path)
    golden._code_identity.cache_clear()

    assert before != after, "an upgraded dud reused the old guest's snapshot"


def test_verify_refuses_a_snapshot_booted_from_other_bits(tmp_path):
    """What the key still cannot see: an image TAG that now resolves to
    new bytes, or a moved bundled kernel. Neither changes a kwarg, so
    both land on an unchanged fingerprint — and the resolved rootfs
    path, being content-addressed, is what gives them away."""
    d = _published(tmp_path / "g")
    golden.verify(d, _ROOTFS, _KERNEL)  # the bits it was booted from

    with pytest.raises(golden.StaleGolden, match="rootfs"):
        golden.verify(d, Path("/artifacts/rootfs-bbbbbbbb"), _KERNEL)
    with pytest.raises(golden.StaleGolden, match="kernel"):
        golden.verify(d, _ROOTFS, Path("/kernels/amd64/Image.new"))


def test_verify_refuses_a_snapshot_with_no_manifest(tmp_path):
    """A directory from a dud that predates the manifest says nothing
    about its provenance, so it cannot be trusted rather than merely
    being assumed current."""
    d = _frozen_rundir(tmp_path / "g")
    with pytest.raises(golden.StaleGolden):
        golden.verify(d, _ROOTFS, _KERNEL)


def test_scratch_configs_are_not_eligible():
    """A snapshot records the absolute path of every backing file. The
    rootfs and caller-supplied disks live at paths that outlive the
    session; a scratch volume is cloned per boot into the seed's own
    rundir and dies with it. Restoring one would reference a file that
    is gone — and since a failed restore discards and reseeds, that is
    a permanent loop rather than one slow boot."""
    assert golden.eligible({"image": "x"})
    assert golden.eligible({"image": "x", "disks": ["/vol/data.img"]})
    assert not golden.eligible({"image": "x", "scratch": "/vol/cache.img"})
