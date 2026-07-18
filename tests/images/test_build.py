"""End-to-end build + spec-hash caching, with the registry stubbed out."""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from dud.images import builder as buildmod
from dud.images.registry import ImageRef, PulledImage


@pytest.fixture
def stub_image(make_layer, monkeypatch):
    """Make ``Registry.pull`` return a fixed synthetic python-like image."""
    layer = make_layer(
        "base",
        dirs=["usr/local/lib/python3.12/site-packages", "usr/local/bin"],
        files={"etc/os-release": "ID=debian"},
        symlinks={"usr/local/bin/python3": "python3.12"},
    )
    img = PulledImage(
        ref=ImageRef.parse("python:3.12-slim"),
        digest="sha256:" + "b" * 64,
        config={"config": {"Env": ["PATH=/usr/local/bin"], "WorkingDir": "/"}},
        layer_paths=[layer],
    )
    monkeypatch.setattr(buildmod.registry.Registry, "pull",
                        lambda self, ref, arch=None: img)
    return img


def test_build_produces_bootable_rootfs(tmp_path, stub_image):
    home = tmp_path / "home"
    r = buildmod.build("python:3.12-slim", home=home)

    assert r.rootfs_path.exists()
    assert r.meta_path.exists()
    assert r.env == ["PATH=/usr/local/bin"]

    raw = gzip.decompress(r.rootfs_path.read_bytes())
    assert raw[:6] == b"070701"  # newc magic
    # The injected runtime and entrypoint made it in.
    assert b"dud/guest/supervisor.py" in raw
    assert b"from dud.guest.init import main" in raw


def test_build_is_cached(tmp_path, stub_image, monkeypatch):
    home = tmp_path / "home"
    r1 = buildmod.build("python:3.12-slim", home=home)
    mtime1 = r1.rootfs_path.stat().st_mtime_ns

    # Second build with the same spec must not rewrite the artifact.
    calls = {"n": 0}
    real = buildmod.rootfs.build_fileset
    monkeypatch.setattr(buildmod.rootfs, "build_fileset",
                        lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1)
                                         or real(*a, **k)))
    r2 = buildmod.build("python:3.12-slim", home=home)
    assert r2.spec == r1.spec
    assert calls["n"] == 0
    assert r2.rootfs_path.stat().st_mtime_ns == mtime1


def test_force_rebuilds(tmp_path, stub_image):
    home = tmp_path / "home"
    buildmod.build("python:3.12-slim", home=home)
    r = buildmod.build("python:3.12-slim", home=home, force=True)
    assert r.rootfs_path.exists()


def test_spec_hash_tracks_workspace_medium_and_code():
    base = buildmod._spec_hash("sha256:x", "/workspace", "initramfs")
    assert base != buildmod._spec_hash("sha256:x", "/data", "initramfs")
    assert base != buildmod._spec_hash("sha256:y", "/workspace", "initramfs")
    assert base != buildmod._spec_hash("sha256:x", "/workspace", "ext4")
    assert base == buildmod._spec_hash("sha256:x", "/workspace", "initramfs")


def test_meta_records_medium(tmp_path, stub_image):
    import json

    r = buildmod.build("python:3.12-slim", home=tmp_path / "home")
    meta = json.loads(r.meta_path.read_text())
    assert meta["medium"] == "initramfs"
    assert meta["artifact"] == "rootfs.cpio.gz"
    assert r.medium == "initramfs"


def test_unknown_medium_rejected(tmp_path, stub_image):
    with pytest.raises(ValueError):
        buildmod.build("python:3.12-slim", home=tmp_path / "home", medium="btrfs")
