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


def test_spec_hash_tracks_workspace_medium_packages_and_code():
    base = buildmod._spec_hash("sha256:x", "/workspace", "initramfs", ())
    assert base != buildmod._spec_hash("sha256:x", "/data", "initramfs", ())
    assert base != buildmod._spec_hash("sha256:y", "/workspace", "initramfs", ())
    assert base != buildmod._spec_hash("sha256:x", "/workspace", "ext4", ())
    assert base != buildmod._spec_hash("sha256:x", "/workspace", "initramfs", ("numpy",))
    assert base == buildmod._spec_hash("sha256:x", "/workspace", "initramfs", ())


def test_meta_records_medium(tmp_path, stub_image):
    import json

    r = buildmod.build("python:3.12-slim", home=tmp_path / "home")
    meta = json.loads(r.meta_path.read_text())
    assert meta["medium"] == "initramfs"
    assert meta["artifact"] == "rootfs.cpio.gz"
    assert meta["packages"] == []
    assert r.medium == "initramfs"


def test_build_layers_packages(tmp_path, stub_image, monkeypatch):
    """packages= folds resolved wheels into site-packages and records them
    (wheel resolution is stubbed — no uv/network)."""
    import gzip
    import json
    from pathlib import Path

    from dud.images import wheels

    def fake_resolve(packages, dest, arch, py):
        pkg = Path(dest) / "fakepkg"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("VALUE = 1\n")
        return dest

    monkeypatch.setattr(wheels, "resolve_wheels", fake_resolve)
    r = buildmod.build("python:3.12-slim", home=tmp_path / "home",
                       packages=["fakepkg"])
    meta = json.loads(r.meta_path.read_text())
    assert meta["packages"] == ["fakepkg"]
    raw = gzip.decompress(r.rootfs_path.read_bytes())
    assert b"site-packages/fakepkg/__init__.py" in raw


def test_unknown_medium_rejected(tmp_path, stub_image):
    with pytest.raises(ValueError):
        buildmod.build("python:3.12-slim", home=tmp_path / "home", medium="btrfs")


def test_fileset_tar_round_trips_modes_and_symlinks(tmp_path):
    import io
    import tarfile

    from dud.images.builder import _fileset_tar
    from dud.images.cpio import FileSet

    fs = FileSet()
    fs.add_file("usr/local/bin/tool", b"#!x", perm=0o755)
    fs.add_file("etc/conf", b"c", perm=0o644)
    fs.add_symlink("etc/rel", "conf")
    fs.add_symlink("etc/abs", "/proc/self/mounts")  # absolute -> relative
    tar = _fileset_tar(fs, prefix="src")
    with tarfile.open(fileobj=io.BytesIO(tar)) as tf:
        m = {i.name: i for i in tf.getmembers()}
        assert m["src/usr/local/bin/tool"].mode & 0o111
        assert m["src/etc/rel"].linkname == "conf"
        assert m["src/etc/abs"].linkname == "../proc/self/mounts"
        assert m["src/usr"].isdir()
    # extraction-safe under the strict filter used by push_tree
    with tarfile.open(fileobj=io.BytesIO(tar)) as tf:
        tf.extractall(tmp_path, filter="data")
    assert (tmp_path / "src/etc/conf").read_bytes() == b"c"


def test_resolve_medium_auto(tmp_path):
    from dud.images.builder import _resolve_medium

    class Img:
        def __init__(self, sizes):
            self.layer_paths = []
            for i, size in enumerate(sizes):
                p = tmp_path / f"l{i}"
                p.write_bytes(b"\0" * size)
                self.layer_paths.append(p)

    small = Img([10_000_000])
    big = Img([90_000_000, 60_000_000])
    assert _resolve_medium("auto", small, ()) == "initramfs"
    assert _resolve_medium("auto", big, ()) == "erofs"
    assert _resolve_medium("auto", small, ("pandas",)) == "erofs"
    assert _resolve_medium("initramfs", big, ("pandas",)) == "initramfs"
    assert _resolve_medium("erofs", small, ()) == "erofs"
