"""Shared VM-rung logic that needs no VM (runs anywhere, incl. CI).

These moved here with the code: kernel resolution, medium cmdline,
scratch device naming and the rundir sweep are the same on every VM
rung, and they were only ever filed under vfkit because that rung
landed first. The sweep in particular reaps FIRECRACKER snapshot
bundles, which made its old home actively misleading.
"""

from __future__ import annotations

import pytest

from dud.backends import vm
from dud.errors import IsolationUnavailable


def test_host_arch_normalizes():
    assert vm._host_arch() in ("arm64", "amd64")


def test_resolve_kernel_explicit_arg(tmp_path):
    k = tmp_path / "Image"
    k.write_bytes(b"kernel")
    assert vm._resolve_kernel(k, "arm64", tmp_path) == k


def test_resolve_kernel_env(tmp_path, monkeypatch):
    k = tmp_path / "envkernel"
    k.write_bytes(b"kernel")
    monkeypatch.setenv("DUD_KERNEL", str(k))
    assert vm._resolve_kernel(None, "arm64", tmp_path) == k


def test_resolve_kernel_home_default(tmp_path, monkeypatch):
    monkeypatch.delenv("DUD_KERNEL", raising=False)
    k = tmp_path / "kernels" / "arm64" / "Image"
    k.parent.mkdir(parents=True)
    k.write_bytes(b"kernel")
    assert vm._resolve_kernel(None, "arm64", tmp_path) == k


def test_resolve_kernel_missing_fails_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("DUD_KERNEL", raising=False)
    with pytest.raises(IsolationUnavailable):
        vm._resolve_kernel(None, "arm64", tmp_path)


def test_medium_cmdline():
    assert vm._medium_cmdline("initramfs") == ""
    extra = vm._medium_cmdline("erofs")
    assert "root=/dev/vda" in extra and "init=/init" in extra
    assert "ro" in extra.split() and "rootwait" in extra.split()


def test_scratch_device_names_by_medium_and_disks():
    """Scratch attaches last: after the erofs root device (if any) and
    any extra disks."""
    assert vm._scratch_device("initramfs", 0) == "/dev/vda"
    assert vm._scratch_device("initramfs", 2) == "/dev/vdc"
    assert vm._scratch_device("erofs", 0) == "/dev/vdb"
    assert vm._scratch_device("erofs", 2) == "/dev/vdd"


def _rundir(tmp_path, name, pid=None, age=0.0):
    import os
    import time

    d = tmp_path / (vm._RUNDIR_PREFIX + name)
    d.mkdir()
    if pid is not None:
        (d / "pid").write_text(str(pid))
    if age:
        old = time.time() - age
        os.utime(d, (old, old))
    return d


def test_sweep_removes_dir_with_dead_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(vm, "_vmm_alive", lambda pid, rd: False)
    d = _rundir(tmp_path, "dead", pid=12345)
    removed = vm.sweep_stale_rundirs(tmp_path)
    assert removed == [str(d)] and not d.exists()


def test_sweep_keeps_dir_with_live_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(vm, "_vmm_alive", lambda pid, rd: True)
    d = _rundir(tmp_path, "live", pid=12345)
    assert vm.sweep_stale_rundirs(tmp_path) == [] and d.exists()


def test_sweep_spares_young_pidless_dir(tmp_path):
    """No pidfile + young = a concurrent boot mid-setup, not a crash."""
    d = _rundir(tmp_path, "booting")
    assert vm.sweep_stale_rundirs(tmp_path) == [] and d.exists()


def test_sweep_removes_old_pidless_dir(tmp_path):
    """No pidfile + old = a host that died between mkdtemp and Popen."""
    d = _rundir(tmp_path, "wreck", age=3600.0)
    removed = vm.sweep_stale_rundirs(tmp_path)
    assert removed == [str(d)] and not d.exists()


def test_sweep_ignores_unrelated_dirs(tmp_path):
    other = tmp_path / "not-a-vm-dir"
    other.mkdir()
    vm.sweep_stale_rundirs(tmp_path)
    assert other.exists()


def test_vmm_alive_rejects_dead_and_reused_pids():
    import os
    import subprocess
    import sys

    assert vm._vmm_alive(2 ** 30, "/tmp/dud-vm-x") is False
    # A live pid whose command has nothing to do with the rundir is a
    # pid-reuse collision, not our vm.
    assert vm._vmm_alive(os.getpid(), "/tmp/dud-vm-x") is False
    # A live process whose argv carries the rundir counts as serving it.
    marker = "/tmp/dud-vm-alive-test"
    p = subprocess.Popen([sys.executable, "-c",
                          f"import time; time.sleep(30)  # {marker}"])
    try:
        # ps can catch the child between fork and exec (argv not yet
        # the marker-bearing one) — poll briefly rather than flake.
        import time

        deadline = time.monotonic() + 5.0
        while not vm._vmm_alive(p.pid, marker):
            assert time.monotonic() < deadline, "argv never showed marker"
            time.sleep(0.05)
    finally:
        p.kill()
        p.wait()


def test_sweep_keeps_frozen_dir_with_live_owner(tmp_path):
    """A frozen park (firecracker snapshot) has no VMM pid — the
    marker's HOST pid is what keeps the sweep off the bundle."""
    import os

    d = _rundir(tmp_path, "frozen-live")
    (d / "frozen").write_text(str(os.getpid()))
    assert vm.sweep_stale_rundirs(tmp_path) == [] and d.exists()


def test_sweep_removes_frozen_dir_with_dead_owner(tmp_path):
    d = _rundir(tmp_path, "frozen-dead", pid=12345)
    (d / "frozen").write_text("999999999")  # beyond pid_max everywhere
    removed = vm.sweep_stale_rundirs(tmp_path)
    assert removed == [str(d)] and not d.exists()


def test_sweep_removes_frozen_dir_with_garbage_marker(tmp_path):
    d = _rundir(tmp_path, "frozen-junk")
    (d / "frozen").write_text("not-a-pid")
    removed = vm.sweep_stale_rundirs(tmp_path)
    assert removed == [str(d)] and not d.exists()


def test_sweep_freezing_marker_live_owner_is_untouchable(tmp_path):
    """Mid-freeze (host paused the VMM, hasn't killed it yet): the
    bundle belongs to a live host and must not be touched."""
    import os

    d = _rundir(tmp_path, "freezing-live")
    (d / "freezing").write_text(f"{os.getpid()} 12345")
    assert vm.sweep_stale_rundirs(tmp_path) == [] and d.exists()


def test_sweep_freezing_dead_owner_kills_orphaned_vmm(tmp_path, monkeypatch):
    """The one hole process-linkage can't cover: a host that died
    between Pause and the VMM kill leaves a paused VMM that can NEVER
    see EOF. The sweep must finish the job."""
    import signal as sigmod

    killed = []
    monkeypatch.setattr(vm, "_vmm_alive",
                        lambda pid, rd: pid == 4242)
    monkeypatch.setattr(vm.os, "kill", lambda pid, sig: (
        killed.append((pid, sig)) if sig == sigmod.SIGKILL
        else (_ for _ in ()).throw(ProcessLookupError())  # owner probe
    ))
    d = _rundir(tmp_path, "freezing-dead")
    (d / "freezing").write_text("999999999 4242")
    removed = vm.sweep_stale_rundirs(tmp_path)
    assert removed == [str(d)] and not d.exists()
    assert killed == [(4242, sigmod.SIGKILL)]


def test_sweep_freezing_dead_owner_dead_vmm_reaps(tmp_path, monkeypatch):
    monkeypatch.setattr(vm, "_vmm_alive", lambda pid, rd: False)
    d = _rundir(tmp_path, "freezing-both-dead")
    (d / "freezing").write_text("999999999 999999998")
    removed = vm.sweep_stale_rundirs(tmp_path)
    assert removed == [str(d)] and not d.exists()



# ---- boot failure must not leak the VMM --------------------------------


def test_configure_failure_kills_the_spawned_vmm(monkeypatch, tmp_path):
    """A rung configured over an API after spawn (firecracker) can fail
    with a live VMM behind it.

    That is why spawn and configure are separate hooks: the base records
    `self._proc` between them, so the cleanup below has something to
    kill. Combined, the process stayed a local, cleanup could not see
    it, and the rundir was removed around a VMM still running and no
    longer reachable by the sweep — an orphan with no marker.
    """
    import os
    import subprocess

    from dud.backends.vm import BootSpec, VmSession

    killed = []

    class _FakeProc:
        pid = 4242
        returncode = None

        def poll(self):
            return None

        def terminate(self):
            killed.append("terminate")
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            killed.append("kill")

    class _Rung(VmSession):
        def _preflight(self):
            pass

        def _listen_path(self):
            # Under /tmp, not tmp_path: pytest's tmp dirs blow past the
            # 104-char AF_UNIX sun_path cap on macOS, which is the same
            # reason the real rundir is anchored there.
            return os.path.join(self._rundir, "sock")

        def _console_arg(self):
            return "console=none"

        def _spawn_vmm(self, spec: BootSpec):
            return _FakeProc()

        def _configure_vmm(self, spec: BootSpec):
            raise RuntimeError("machine-config rejected")

    monkeypatch.setattr(
        "dud.backends.vm.build_rootfs",
        lambda *a, **k: type("B", (), {
            "rootfs_path": tmp_path / "rootfs", "medium": "initramfs",
        })(),
    )
    monkeypatch.setattr(
        "dud.backends.vm._resolve_kernel", lambda *a, **k: tmp_path / "Image"
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)

    with pytest.raises(IsolationUnavailable, match="machine-config rejected"):
        _Rung(home=tmp_path)
    assert killed, "a VMM live at configure time must be torn down"


# ---- module CLIs -------------------------------------------------------


def test_module_clis_are_importable_and_parse_args():
    """`python -m dud.kernels` is the documented install step for both
    VM rungs — the first command a user runs after `pip install dud`.

    Its `_host_arch` import moved with the VmSession extraction and
    nothing noticed: 348 tests passed while the entrypoint raised
    ImportError before argparse ever ran. A suite that never invokes a
    CLI cannot see a CLI break.
    """
    import subprocess
    import sys

    for mod in ("dud.kernels", "dud.images.builder"):
        r = subprocess.run(
            [sys.executable, "-m", mod, "--help"],
            capture_output=True, text=True,
        )
        assert r.returncode == 0, f"{mod} --help failed:\n{r.stderr}"
        assert "usage:" in r.stdout
