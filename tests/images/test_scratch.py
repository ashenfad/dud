"""Scratch volume bake logic that needs no VM."""

from __future__ import annotations

import gzip

from dud.images import scratch


def test_unpack_sparse_roundtrip(tmp_path):
    # Hole sizes mirror reality (mke2fs zero runs are tens of MB or
    # more): APFS backfills seek-holes below its allocation
    # granularity, so small holes wouldn't test sparseness.
    size = 96 * (1 << 20)
    raw = bytearray(size)
    raw[0:5] = b"hdr!!"
    raw[(48 << 20):(48 << 20) + 4] = b"data"
    gz = gzip.compress(bytes(raw), compresslevel=1)
    dest = tmp_path / "img"
    scratch._unpack_sparse(gz, dest, size)
    got = dest.read_bytes()
    assert len(got) == size
    assert got[0:5] == b"hdr!!"
    assert got[(48 << 20):(48 << 20) + 4] == b"data"
    assert got[(24 << 20):(24 << 20) + 16] == b"\0" * 16
    # The zero runs landed (mostly) as holes, not blocks.
    assert dest.stat().st_blocks * 512 < size // 2


def test_unpack_sparse_trailing_hole_reaches_full_size(tmp_path):
    size = 2 * (1 << 20)
    raw = bytearray(size)
    raw[0:3] = b"top"  # everything after the first chunk is a hole
    dest = tmp_path / "img"
    scratch._unpack_sparse(gzip.compress(bytes(raw)), dest, size)
    assert dest.stat().st_size == size


def test_blank_ext4_caches_by_size(tmp_path, monkeypatch):
    calls: list[int] = []

    def fake_bake(tool, tmp, size_mib):
        calls.append(size_mib)
        tmp.write_bytes(b"fake image")

    monkeypatch.setattr(scratch, "_host_mke2fs", lambda: "/bin/fake-mke2fs")
    monkeypatch.setattr(scratch, "_bake_host", fake_bake)
    p1 = scratch.blank_ext4(64, home=tmp_path)
    p2 = scratch.blank_ext4(64, home=tmp_path)
    assert p1 == p2 and calls == [64]  # second call served from cache
    scratch.blank_ext4(128, home=tmp_path)
    assert calls == [64, 128]  # sizes cache independently
