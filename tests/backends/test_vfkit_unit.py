"""vfkit-specific logic that needs no VM (runs anywhere, incl. CI).

What is left here after the VmSession extraction is genuinely vfkit's:
how *this* VMM is told to provide a rootfs, and the platform preflight.
Everything shared with the firecracker rung — kernel resolution, medium
cmdline, scratch device naming, the rundir sweep — lives in
``test_vm_unit.py`` alongside the code.
"""

from __future__ import annotations

import pytest

from dud.backends import vfkit
from dud.backends.vfkit import VfkitSession
from dud.backends.vm import BootSpec
from dud.errors import IsolationUnavailable


@pytest.fixture
def runnable_rung(monkeypatch, tmp_path):
    """Get past the preflight so a test can reach what it is about.

    The argument-validation tests below care about `disks=` and
    `scratch=`, not about this host. Faking the two preflight facts —
    macOS, and a vfkit binary — lets them assert their actual subject
    on any platform, and stops a CI runner without vfkit installed
    from reporting "vfkit not found" as though it were the finding.

    Safe on Linux: validation raises before anything is pulled, built
    or booted, so no VM is ever created.
    """
    monkeypatch.setattr(vfkit.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(vfkit, "_vfkit_bin", lambda: "/usr/bin/true")
    monkeypatch.setenv("DUD_HOME", str(tmp_path))


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


def test_missing_disk_image_fails_closed(tmp_path, runnable_rung):
    """disks= paths are validated before any VM resources are spent."""
    with pytest.raises(IsolationUnavailable, match="disk image not found"):
        VfkitSession(disks=[tmp_path / "nope.erofs"])


def test_missing_scratch_volume_fails_closed(tmp_path, runnable_rung):
    with pytest.raises(IsolationUnavailable, match="scratch volume not found"):
        VfkitSession(scratch=tmp_path / "nope.ext4")


def test_missing_vfkit_fails_closed_before_any_build(monkeypatch, tmp_path):
    """The preflight ordering itself, which CI caught and a developer
    machine cannot: with vfkit installed everywhere locally, nothing
    here exercises its absence.

    It has to raise before `build_rootfs`, or a host with no VMM pays
    for a registry pull and a rootfs build to be told it was never
    going to boot.
    """
    monkeypatch.setattr(vfkit.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(vfkit.shutil, "which", lambda _n: None)
    monkeypatch.setattr(vfkit.Path, "exists", lambda _self: False)
    monkeypatch.setenv("DUD_HOME", str(tmp_path))

    import dud.backends.vm as vmmod

    def _no_build(*a, **k):
        raise AssertionError("preflight must raise before any build")

    monkeypatch.setattr(vmmod, "build_rootfs", _no_build)
    with pytest.raises(IsolationUnavailable, match="vfkit not found"):
        VfkitSession()
