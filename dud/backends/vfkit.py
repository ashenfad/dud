"""Rung 2 (macOS): the guest supervisor inside a vfkit microVM.

Same guest, same wire protocol as rung 1 — only the transport changes.
The host listens on a short-path unix socket; vfkit boots the rootfs from
:mod:`dud.images`, and the guest's ``dud.guest.init`` dials back over
vsock (guest connects to CID 2; vfkit bridges that to our unix socket).
Once the channel is up it is an ordinary :class:`HostSession`.

Boot facts settled by the stage-4 spikes (see DESIGN.md):
  - vsock direction is guest->host: the guest dials CID 2
    (``dud.mode=connect``) and the vsock device's ``connect`` qualifier
    makes vfkit forward that to the unix socket the host listens on.
    (vfkit's default is host->guest and drops a guest-initiated dial.)
  - the kernel is a versioned dud asset (arch-matched uncompressed
    ``Image``; see :mod:`dud.kernels`), not shipped by the image. The
    pinned kernel has virtio-rng built in, so entropy is real — the
    old puipui kernel needed a ``PYTHONHASHSEED=0`` cmdline workaround.
  - rootfs medium comes from ``meta.json`` — only ``initramfs`` is wired;
    ``erofs`` (virtio-blk, read-only by construction) is the additive
    large-image path.

Requesting this rung where it can't run fails closed
(:class:`IsolationUnavailable`) rather than silently degrading.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket as socketlib
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from ..images import build as build_rootfs, dud_home
from ..proto import Channel
from .base import HostSession

_VSOCK_PORT = 1024
_HOST_CID = 2


class IsolationUnavailable(RuntimeError):
    """The requested VM rung can't run here (platform/tooling/kernel)."""


def _host_arch() -> str:
    m = platform.machine().lower()
    return "arm64" if m in ("arm64", "aarch64") else "amd64"


def _resolve_kernel(kernel: str | Path | None, arch: str, home: Path) -> Path:
    """Kernel lookup: explicit arg -> $DUD_KERNEL -> ~/.dud/kernels/<arch>."""
    for cand in (kernel, os.environ.get("DUD_KERNEL"),
                 home / "kernels" / arch / "Image"):
        if cand:
            p = Path(cand)
            if p.is_file():
                return p
    raise IsolationUnavailable(
        f"no guest kernel for {arch}: run `python -m dud.kernels` to fetch "
        f"the pinned one, pass kernel=, set $DUD_KERNEL, or place an "
        f"uncompressed Image at {home / 'kernels' / arch / 'Image'}"
    )


def _vfkit_bin() -> str:
    exe = shutil.which("vfkit") or "/opt/homebrew/bin/vfkit"
    if not Path(exe).exists():
        raise IsolationUnavailable("vfkit not found (brew install vfkit)")
    return exe


_RUNDIR_PREFIX = "dud-vm-"
_swept = False


def _vfkit_alive(pid: int, rundir: str) -> bool:
    """Is ``pid`` a live vfkit serving ``rundir``? The command-line
    check guards against pid reuse: every vfkit invocation carries its
    rundir in its args (socketURL/console paths)."""
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True,
        )
    except OSError:
        return False
    return rundir in out.stdout


def sweep_stale_rundirs(root: str | Path = "/tmp") -> list[str]:
    """Remove rundirs (sockets, logs, APFS rootfs clones) orphaned by a
    host that died hard. Processes can't dangle — channel EOF powers
    the guest off and vfkit exits with it — but their on-disk rundirs
    can. A dir whose recorded vfkit pid is live is someone else's
    running VM and is left alone; one with no pidfile is only removed
    once it's old enough (10 min) to rule out a concurrent mid-boot."""
    removed: list[str] = []
    for path in Path(root).glob(_RUNDIR_PREFIX + "*"):
        pidfile = path / "pid"
        try:
            pid = int(pidfile.read_text())
        except (OSError, ValueError):
            try:
                age = time.time() - path.stat().st_mtime
            except OSError:
                continue
            if age < 600:
                continue
        else:
            if _vfkit_alive(pid, str(path)):
                continue
        shutil.rmtree(path, ignore_errors=True)
        removed.append(str(path))
    return removed


def _sweep_once() -> None:
    global _swept
    if not _swept:
        _swept = True
        try:
            sweep_stale_rundirs()
        except OSError:
            pass  # hygiene, never a boot blocker


def _medium_boot_args(rootfs_path: Path, medium: str, rundir: str) -> list[str]:
    """VMM args that provide the rootfs, chosen by its medium."""
    if medium == "initramfs":
        return ["--initrd", str(rootfs_path)]
    if medium == "erofs":
        # First virtio-blk device: the kernel mounts it as / directly
        # (see _medium_cmdline); demand-paged — RAM is pages touched,
        # not image size. Each VM attaches a per-boot APFS clone
        # (instant CoW, zero extra disk): VZ takes an exclusive lock on
        # a read-write attachment, so concurrent VMs can't share one
        # file — and vfkit's virtio-blk exposes no readOnly flag even
        # though the VZ API has one (upstream opportunity; a readonly
        # attach would also restore cross-VM page-cache sharing).
        # The EMPTY initrd is a vfkit-CLI appeasement (its
        # kernel/initrd/cmdline flags are an all-or-nothing group though
        # VZ itself makes initrd optional); the kernel finds no /init in
        # it and falls through to root=.
        from ..images.cpio import FileSet, build_cpio_gz

        clone = Path(rundir) / rootfs_path.name
        # cp -c = APFS clonefile (instant CoW). Fall back to a real
        # copy where the flag doesn't exist (GNU cp) or cloning fails
        # (non-APFS volume) — correctness first, speed when available.
        cloned = subprocess.run(
            ["cp", "-c", str(rootfs_path), str(clone)],
            capture_output=True,
        )
        if cloned.returncode != 0:
            shutil.copyfile(rootfs_path, clone)
        dummy = Path(rundir) / "empty.cpio.gz"
        dummy.write_bytes(build_cpio_gz(FileSet()))
        return [
            "--initrd", str(dummy),
            "--device", f"virtio-blk,path={clone}",
        ]
    raise IsolationUnavailable(f"unknown rootfs medium {medium!r}")


def _medium_cmdline(medium: str) -> str:
    """Extra kernel cmdline for the medium (appended to the dud.* set)."""
    if medium == "erofs":
        # rootwait: virtio-blk probes async; don't panic before /dev/vda.
        # init=/init: on a real root the kernel would look for
        # /sbin/init — our entrypoint keeps its initramfs name.
        return " root=/dev/vda rootfstype=erofs ro rootwait init=/init"
    return ""


class VfkitSession(HostSession):
    """A workspace session backed by a disposable vfkit microVM."""

    def __init__(
        self,
        image: str = "python:3.12-slim",
        arch: str | None = None,
        workspace: str = "/workspace",
        kernel: str | Path | None = None,
        memory_mib: int = 2048,
        cpus: int = 2,
        home: str | Path | None = None,
        boot_timeout: float = 30.0,
        packages: list[str] | None = None,
        debs: list[str] | None = None,
        disks: list[str | Path] | None = None,
        medium: str = "initramfs",
        host_objects: dict[str, Any] | None = None,
        allow: dict[str, set[str]] | None = None,
        cache: dict[str, bytes] | None = None,
        on_emit: Callable[[str, Any], None] | None = None,
    ):
        super().__init__(host_objects, allow, cache, on_emit)
        if platform.system() != "Darwin":
            raise IsolationUnavailable("vfkit rung requires macOS (HVF)")
        for disk in disks or []:
            # Validate up front: fail before any pull/build work is spent.
            if not Path(disk).is_file():
                raise IsolationUnavailable(f"disk image not found: {disk}")
        # Pooling hooks (see backends/pool.py): when a pool owns this VM,
        # close() parks it there instead of powering off; _pool_kwargs is
        # the boot fingerprint source. park_state (stamped by the owner
        # before close) tags the parked tree's content identity;
        # resumed=True on acquire means the tree already matches and the
        # owner may skip its push.
        self._pool: Any = None
        self.park_state: str | None = None
        self.resumed = False
        self._pool_kwargs = {
            "image": image, "arch": arch, "workspace": workspace,
            "kernel": kernel, "memory_mib": memory_mib, "cpus": cpus,
            "home": home, "packages": packages, "debs": debs,
            "disks": [str(d) for d in disks] if disks else None,
            "medium": medium,
        }
        home = Path(home) if home else dud_home()
        arch = arch or _host_arch()

        self.build = build_rootfs(
            image, arch=arch, workspace=workspace, home=home,
            packages=packages, debs=debs, medium=medium,
        )
        kernel_path = _resolve_kernel(kernel, arch, home)
        vfkit = _vfkit_bin()

        # Short rundir: macOS AF_UNIX sun_path is capped at 104 chars, and
        # $TMPDIR is long, so anchor under /tmp explicitly.
        _sweep_once()
        self._rundir = tempfile.mkdtemp(dir="/tmp", prefix=_RUNDIR_PREFIX)
        self._sock_path = os.path.join(self._rundir, "vsock")
        self._console = os.path.join(self._rundir, "console.log")

        # vsock direction is guest->host. Per vfkit's default (`listen`) the
        # host listens on the unix socket and the guest connects to CID 2;
        # vfkit bridges the guest's dial to our listener. socketURL is a
        # BARE path (no unix:// scheme — vfkit treats a scheme as part of
        # the path). Listen before boot so the guest's early dial has a peer.
        self._srv = socketlib.socket(socketlib.AF_UNIX, socketlib.SOCK_STREAM)
        self._srv.bind(self._sock_path)
        self._srv.listen(1)

        cmdline = (
            f"console=hvc0 random.trust_cpu=on "
            f"dud.mode=connect dud.cid={_HOST_CID} dud.port={_VSOCK_PORT} "
            f"dud.root={workspace}"
        ) + _medium_cmdline(self.build.medium)
        args = [
            vfkit, "--cpus", str(cpus), "--memory", str(memory_mib),
            "--kernel", str(kernel_path),
            "--kernel-cmdline", cmdline,
            *_medium_boot_args(self.build.rootfs_path, self.build.medium,
                               self._rundir),
            "--device", "virtio-rng",
            "--device", f"virtio-serial,logFilePath={self._console}",
            "--device",
            f"virtio-vsock,port={_VSOCK_PORT},socketURL={self._sock_path}",
        ]
        # Extra block devices (read-only artifacts: erofs workspace
        # images, published-app snapshots). Guest order: extras follow
        # the rootfs device — /dev/vda.. on initramfs, /dev/vdb.. when
        # the root itself is a block device (erofs).
        for disk in disks or []:
            args += ["--device", f"virtio-blk,path={Path(disk)}"]
        self._vfkit_log = open(os.path.join(self._rundir, "vfkit.log"), "wb")
        self._proc = subprocess.Popen(args, stdout=self._vfkit_log,
                                      stderr=subprocess.STDOUT)
        # Liveness record for sweep_stale_rundirs (a future host process
        # cleaning up after a crash of THIS one).
        Path(self._rundir, "pid").write_text(str(self._proc.pid))

        conn = self._accept(boot_timeout)
        self._ch = Channel(conn, handler=self._handle)
        self._ch.hello_recv()

    def _accept(self, timeout: float) -> socketlib.socket:
        """Wait for vfkit to bridge the guest's outbound vsock connection."""
        self._srv.settimeout(timeout)
        try:
            conn, _ = self._srv.accept()
            return conn
        except (socketlib.timeout, OSError) as e:
            self._teardown_vm()
            tail = self._console_tail()  # read before the rundir goes away
            try:
                self._srv.close()
            except OSError:
                pass
            shutil.rmtree(self._rundir, ignore_errors=True)
            raise IsolationUnavailable(
                f"guest did not connect within {timeout}s ({e}); console tail:\n"
                + tail
            )

    def _console_tail(self, n: int = 25) -> str:
        try:
            lines = Path(self._console).read_text(errors="replace").splitlines()
            return "\n".join(lines[-n:])
        except OSError:
            return "(no console output)"

    # ---- teardown ------------------------------------------------------

    def _teardown_vm(self) -> None:
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        try:
            self._vfkit_log.close()
        except Exception:
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._pool is not None:
            # Pooled: park the warm VM for the next session (the pool
            # resets the guest; a failed reset tears the VM down).
            self._pool.release(self)
            return
        # shutdown verb -> supervisor stops serving -> init powers the VM off.
        try:
            self._ch.request("shutdown")
        except Exception:
            pass
        try:
            self._ch.close()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self._teardown_vm()
        for closeable in (self._srv, self._vfkit_log):
            try:
                closeable.close()
            except Exception:
                pass
        shutil.rmtree(self._rundir, ignore_errors=True)
