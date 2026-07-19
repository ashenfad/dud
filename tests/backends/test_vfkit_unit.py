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


def test_scratch_device_names_by_medium_and_disks():
    """Scratch attaches last: after the erofs root device (if any) and
    any extra disks."""
    assert vfkit._scratch_device("initramfs", 0) == "/dev/vda"
    assert vfkit._scratch_device("initramfs", 2) == "/dev/vdc"
    assert vfkit._scratch_device("erofs", 0) == "/dev/vdb"
    assert vfkit._scratch_device("erofs", 2) == "/dev/vdd"


def test_missing_scratch_volume_fails_closed(tmp_path, monkeypatch):
    import platform

    if platform.system() != "Darwin":
        pytest.skip("vfkit ctor is Darwin-only")
    monkeypatch.setenv("DUD_HOME", str(tmp_path))
    from dud.backends.vfkit import VfkitSession

    with pytest.raises(IsolationUnavailable, match="scratch volume not found"):
        VfkitSession(scratch=tmp_path / "nope.ext4")


def _rundir(tmp_path, name, pid=None, age=0.0):
    import os
    import time

    d = tmp_path / (vfkit._RUNDIR_PREFIX + name)
    d.mkdir()
    if pid is not None:
        (d / "pid").write_text(str(pid))
    if age:
        old = time.time() - age
        os.utime(d, (old, old))
    return d


def test_sweep_removes_dir_with_dead_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(vfkit, "_vfkit_alive", lambda pid, rd: False)
    d = _rundir(tmp_path, "dead", pid=12345)
    removed = vfkit.sweep_stale_rundirs(tmp_path)
    assert removed == [str(d)] and not d.exists()


def test_sweep_keeps_dir_with_live_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(vfkit, "_vfkit_alive", lambda pid, rd: True)
    d = _rundir(tmp_path, "live", pid=12345)
    assert vfkit.sweep_stale_rundirs(tmp_path) == [] and d.exists()


def test_sweep_spares_young_pidless_dir(tmp_path):
    """No pidfile + young = a concurrent boot mid-setup, not a crash."""
    d = _rundir(tmp_path, "booting")
    assert vfkit.sweep_stale_rundirs(tmp_path) == [] and d.exists()


def test_sweep_removes_old_pidless_dir(tmp_path):
    """No pidfile + old = a host that died between mkdtemp and Popen."""
    d = _rundir(tmp_path, "wreck", age=3600.0)
    removed = vfkit.sweep_stale_rundirs(tmp_path)
    assert removed == [str(d)] and not d.exists()


def test_sweep_ignores_unrelated_dirs(tmp_path):
    other = tmp_path / "not-a-vm-dir"
    other.mkdir()
    vfkit.sweep_stale_rundirs(tmp_path)
    assert other.exists()


def test_vfkit_alive_rejects_dead_and_reused_pids():
    import os
    import subprocess
    import sys

    assert vfkit._vfkit_alive(2 ** 30, "/tmp/dud-vm-x") is False
    # A live pid whose command has nothing to do with the rundir is a
    # pid-reuse collision, not our vfkit.
    assert vfkit._vfkit_alive(os.getpid(), "/tmp/dud-vm-x") is False
    # A live process whose argv carries the rundir counts as serving it.
    marker = "/tmp/dud-vm-alive-test"
    p = subprocess.Popen([sys.executable, "-c",
                          f"import time; time.sleep(30)  # {marker}"])
    try:
        assert vfkit._vfkit_alive(p.pid, marker) is True
    finally:
        p.kill()
        p.wait()


def test_sweep_keeps_frozen_dir_with_live_owner(tmp_path):
    """A frozen park (firecracker snapshot) has no VMM pid — the
    marker's HOST pid is what keeps the sweep off the bundle."""
    import os

    d = _rundir(tmp_path, "frozen-live")
    (d / "frozen").write_text(str(os.getpid()))
    assert vfkit.sweep_stale_rundirs(tmp_path) == [] and d.exists()


def test_sweep_removes_frozen_dir_with_dead_owner(tmp_path):
    d = _rundir(tmp_path, "frozen-dead", pid=12345)
    (d / "frozen").write_text("999999999")  # beyond pid_max everywhere
    removed = vfkit.sweep_stale_rundirs(tmp_path)
    assert removed == [str(d)] and not d.exists()


def test_sweep_removes_frozen_dir_with_garbage_marker(tmp_path):
    d = _rundir(tmp_path, "frozen-junk")
    (d / "frozen").write_text("not-a-pid")
    removed = vfkit.sweep_stale_rundirs(tmp_path)
    assert removed == [str(d)] and not d.exists()
