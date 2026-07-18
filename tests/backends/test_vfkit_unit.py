"""vfkit backend logic that needs no VM (runs anywhere, incl. CI)."""

from __future__ import annotations

import pytest

from dud.backends import vfkit
from dud.backends.vfkit import IsolationUnavailable


def test_host_arch_normalizes():
    assert vfkit._host_arch() in ("arm64", "amd64")


def test_medium_boot_args_initramfs(tmp_path):
    rootfs = tmp_path / "rootfs.cpio.gz"
    rootfs.write_bytes(b"x")
    assert vfkit._medium_boot_args(rootfs, "initramfs", str(tmp_path)) == ["--initrd", str(rootfs)]


def test_medium_boot_args_unknown(tmp_path):
    with pytest.raises(IsolationUnavailable):
        vfkit._medium_boot_args(tmp_path / "r", "btrfs", str(tmp_path))


def test_resolve_kernel_explicit_arg(tmp_path):
    k = tmp_path / "Image"
    k.write_bytes(b"kernel")
    assert vfkit._resolve_kernel(k, "arm64", tmp_path) == k


def test_resolve_kernel_env(tmp_path, monkeypatch):
    k = tmp_path / "envkernel"
    k.write_bytes(b"kernel")
    monkeypatch.setenv("DUD_KERNEL", str(k))
    assert vfkit._resolve_kernel(None, "arm64", tmp_path) == k


def test_resolve_kernel_home_default(tmp_path, monkeypatch):
    monkeypatch.delenv("DUD_KERNEL", raising=False)
    k = tmp_path / "kernels" / "arm64" / "Image"
    k.parent.mkdir(parents=True)
    k.write_bytes(b"kernel")
    assert vfkit._resolve_kernel(None, "arm64", tmp_path) == k


def test_resolve_kernel_missing_fails_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("DUD_KERNEL", raising=False)
    with pytest.raises(IsolationUnavailable):
        vfkit._resolve_kernel(None, "arm64", tmp_path)


def test_non_darwin_fails_closed(monkeypatch):
    monkeypatch.setattr(vfkit.platform, "system", lambda: "Linux")
    with pytest.raises(IsolationUnavailable):
        vfkit.VfkitSession()


def test_missing_disk_image_fails_closed(tmp_path, monkeypatch):
    """disks= paths are validated before any VM resources are spent."""
    import platform

    if platform.system() != "Darwin":
        pytest.skip("vfkit ctor is Darwin-only")
    monkeypatch.setenv("DUD_HOME", str(tmp_path))
    from dud.backends.vfkit import VfkitSession

    with pytest.raises(IsolationUnavailable, match="disk image not found"):
        VfkitSession(disks=[tmp_path / "nope.erofs"])


def test_medium_boot_args_erofs_is_block_device(tmp_path):
    img = tmp_path / "rootfs.erofs"
    img.write_bytes(b"e")
    rundir = tmp_path / "run"
    rundir.mkdir()
    args = vfkit._medium_boot_args(img, "erofs", str(rundir))
    assert args[0] == "--initrd" and args[1].endswith("empty.cpio.gz")
    # attaches a per-boot clone in the rundir, not the shared artifact
    clone = rundir / "rootfs.erofs"
    assert args[2:] == ["--device", f"virtio-blk,path={clone}"]
    assert clone.read_bytes() == b"e"
    assert (rundir / "empty.cpio.gz").stat().st_size < 100


def test_medium_cmdline():
    assert vfkit._medium_cmdline("initramfs") == ""
    extra = vfkit._medium_cmdline("erofs")
    assert "root=/dev/vda" in extra and "init=/init" in extra
    assert "ro" in extra.split() and "rootwait" in extra.split()
