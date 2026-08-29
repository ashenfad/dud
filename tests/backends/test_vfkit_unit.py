"""vfkit-specific logic that needs no VM (runs anywhere, incl. CI).

What is left here after the VmSession extraction is genuinely vfkit's:
how *this* VMM is told to provide a rootfs, and the platform preflight.
Everything shared with the firecracker rung — kernel resolution, medium
cmdline, scratch device naming, the rundir sweep — lives in
``test_vm_unit.py`` alongside the code.
"""

from __future__ import annotations

import platform

import pytest

from dud.backends import vfkit
from dud.backends.vfkit import VfkitSession
from dud.backends.vm import BootSpec
from dud.errors import IsolationUnavailable

_DARWIN_ONLY = pytest.mark.skipif(
    platform.system() != "Darwin", reason="vfkit ctor is Darwin-only"
)


def _spec(rootfs, medium, rundir, **kw) -> BootSpec:
    """A BootSpec with only the fields the medium branch reads."""
    defaults = dict(
        kernel=rootfs, cmdline="", rootfs=rootfs, medium=medium, disks=[],
        scratch_clone=None, rundir=str(rundir), console="", listen_path="",
        cpus=2, memory_mib=1024,
    )
    return BootSpec(**{**defaults, **kw})


def test_medium_boot_args_initramfs(tmp_path):
    rootfs = tmp_path / "rootfs.cpio.gz"
    rootfs.write_bytes(b"x")
    args = VfkitSession._medium_boot_args(_spec(rootfs, "initramfs", tmp_path))
    assert args == ["--initrd", str(rootfs)]


def test_medium_boot_args_unknown(tmp_path):
    with pytest.raises(IsolationUnavailable):
        VfkitSession._medium_boot_args(
            _spec(tmp_path / "r", "btrfs", tmp_path)
        )


def test_medium_boot_args_erofs_is_block_device(tmp_path):
    img = tmp_path / "rootfs.erofs"
    img.write_bytes(b"e")
    rundir = tmp_path / "run"
    rundir.mkdir()
    args = VfkitSession._medium_boot_args(_spec(img, "erofs", rundir))
    assert args[0] == "--initrd" and args[1].endswith("empty.cpio.gz")
    # attaches a per-boot clone in the rundir, not the shared artifact
    clone = rundir / "rootfs.erofs"
    assert args[2:] == ["--device", f"virtio-blk,path={clone}"]
    assert clone.read_bytes() == b"e"
    assert (rundir / "empty.cpio.gz").stat().st_size < 100


def test_non_darwin_fails_closed(monkeypatch):
    monkeypatch.setattr(vfkit.platform, "system", lambda: "Linux")
    with pytest.raises(IsolationUnavailable):
        VfkitSession()


@_DARWIN_ONLY
def test_missing_disk_image_fails_closed(tmp_path, monkeypatch):
    """disks= paths are validated before any VM resources are spent."""
    monkeypatch.setenv("DUD_HOME", str(tmp_path))
    with pytest.raises(IsolationUnavailable, match="disk image not found"):
        VfkitSession(disks=[tmp_path / "nope.erofs"])


@_DARWIN_ONLY
def test_missing_scratch_volume_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("DUD_HOME", str(tmp_path))
    with pytest.raises(IsolationUnavailable, match="scratch volume not found"):
        VfkitSession(scratch=tmp_path / "nope.ext4")
