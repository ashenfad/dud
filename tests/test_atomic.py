"""Staging discipline for the shared artifact cache (dud.atomic)."""

from __future__ import annotations

import json
import threading

import pytest

from dud.atomic import part_path, staged, write_json


def test_publish_replaces_dest_in_one_step(tmp_path):
    dest = tmp_path / "artifact.bin"
    dest.write_bytes(b"old")
    with staged(dest) as tmp:
        tmp.write_bytes(b"new")
        assert dest.read_bytes() == b"old"  # not visible until published
    assert dest.read_bytes() == b"new"


def test_failed_write_leaves_dest_and_directory_untouched(tmp_path):
    dest = tmp_path / "artifact.bin"
    dest.write_bytes(b"old")
    with pytest.raises(ValueError):
        with staged(dest) as tmp:
            tmp.write_bytes(b"half a download")
            raise ValueError("digest mismatch")
    assert dest.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [dest]  # no .part residue


def test_staging_paths_are_unique_per_writer(tmp_path):
    """The whole point: concurrent writers never share a staging file."""
    dest = tmp_path / "blobs" / "sha256" / ("a" * 64)
    seen: list[str] = []
    barrier = threading.Barrier(4)

    def writer():
        barrier.wait()
        seen.append(str(part_path(dest)))

    threads = [threading.Thread(target=writer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(seen)) == 4
    assert all(p != str(dest) for p in seen)


def test_staging_path_keeps_the_dest_suffix_intact(tmp_path):
    # with_suffix() would eat ".ext4"; two size classes staging as
    # "master.part.<pid>.<tid>" would then collide with each other.
    a = part_path(tmp_path / "master-4096m.ext4")
    b = part_path(tmp_path / "master-8192m.ext4")
    assert a != b
    assert a.name.startswith("master-4096m.ext4.part.")


def test_write_json_publishes_parseable_content(tmp_path):
    dest = tmp_path / "meta.json"
    write_json(dest, {"spec": "abc", "entries": 3}, indent=2)
    assert json.loads(dest.read_text()) == {"spec": "abc", "entries": 3}
