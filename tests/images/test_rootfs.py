"""Layer flattening, whiteouts, and dud injection."""

from __future__ import annotations

from dud.images import rootfs
from dud.images.cpio import S_IFDIR


def test_later_layer_overrides_earlier(make_layer):
    l1 = make_layer("l1", files={"app/config": "v1"})
    l2 = make_layer("l2", files={"app/config": "v2"})
    fs = rootfs.flatten_layers([l1, l2])
    assert fs.nodes["app/config"].data == b"v2"


def test_whiteout_deletes_file(make_layer):
    l1 = make_layer("l1", files={"app/keep": "1", "app/drop": "2"})
    l2 = make_layer("l2", whiteouts=["app/.wh.drop"])
    fs = rootfs.flatten_layers([l1, l2])
    assert "app/keep" in fs.nodes
    assert "app/drop" not in fs.nodes


def test_opaque_whiteout_clears_directory(make_layer):
    l1 = make_layer("l1", files={"d/old1": "1", "d/old2": "2"})
    l2 = make_layer(
        "l2", whiteouts=["d/.wh..wh..opq"], files={"d/fresh": "3"},
    )
    fs = rootfs.flatten_layers([l1, l2])
    assert "d/old1" not in fs.nodes and "d/old2" not in fs.nodes
    assert fs.nodes["d/fresh"].data == b"3"


def test_symlink_preserved(make_layer):
    l1 = make_layer("l1", symlinks={"usr/bin/py": "python3.12"})
    fs = rootfs.flatten_layers([l1])
    assert fs.nodes["usr/bin/py"].data == b"python3.12"


def test_path_traversal_rejected(make_layer):
    l1 = make_layer("l1", files={"../escape": "x", "ok": "y"})
    fs = rootfs.flatten_layers([l1])
    assert "ok" in fs.nodes
    assert not any("escape" in n for n in fs.nodes)


def test_inject_dud_targets_site_packages(make_layer):
    l1 = make_layer("l1", dirs=["usr/local/lib/python3.12/site-packages"])
    fs = rootfs.flatten_layers([l1])
    site = rootfs.inject_dud(fs)
    assert site == "usr/local/lib/python3.12/site-packages"
    key = f"{site}/dud/guest/supervisor.py"
    assert key in fs.nodes and fs.nodes[key].data


def test_build_fileset_adds_init_and_workspace(make_layer):
    l1 = make_layer("l1", dirs=["usr/local/lib/python3.12/site-packages"])
    from dud.images.registry import PulledImage, ImageRef

    img = PulledImage(
        ref=ImageRef.parse("python:3.12-slim"),
        digest="sha256:deadbeef", config={}, layer_paths=[l1],
    )
    fs = rootfs.build_fileset(img, workspace="/workspace")
    assert fs.nodes["workspace"].mode & S_IFDIR
    init = fs.nodes["init"]
    assert init.mode & 0o111  # executable
    body = init.data.decode()
    assert body.startswith("#!/usr/local/bin/python3")
    assert "from dud.guest.init import main" in body
    assert "default_root='/workspace'" in body
