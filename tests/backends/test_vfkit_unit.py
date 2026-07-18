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
    assert vfkit._medium_boot_args(rootfs, "initramfs") == ["--initrd", str(rootfs)]


def test_medium_boot_args_ext4_not_wired(tmp_path):
    with pytest.raises(IsolationUnavailable):
        vfkit._medium_boot_args(tmp_path / "r.ext4", "ext4")


def test_medium_boot_args_unknown(tmp_path):
    with pytest.raises(IsolationUnavailable):
        vfkit._medium_boot_args(tmp_path / "r", "btrfs")


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
