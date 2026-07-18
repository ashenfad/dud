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
  - ``PYTHONHASHSEED=0`` on the kernel cmdline dodges CPython's
    entropy-at-preinit crash (the kernel forwards ``k=v`` to init's env).
  - the kernel is a bundled dud asset (arch-matched uncompressed ``Image``
    with virtio + vsock), not shipped by the image.
  - rootfs medium comes from ``meta.json`` — only ``initramfs`` is wired;
    ``ext4`` (virtio-blk) is the additive large-image path.

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
        f"no guest kernel for {arch}: pass kernel=, set $DUD_KERNEL, or place "
        f"an uncompressed Image at {home / 'kernels' / arch / 'Image'}"
    )


def _vfkit_bin() -> str:
    exe = shutil.which("vfkit") or "/opt/homebrew/bin/vfkit"
    if not Path(exe).exists():
        raise IsolationUnavailable("vfkit not found (brew install vfkit)")
    return exe


def _medium_boot_args(rootfs_path: Path, medium: str) -> list[str]:
    """VMM args that mount the rootfs, chosen by its medium."""
    if medium == "initramfs":
        return ["--initrd", str(rootfs_path)]
    if medium == "ext4":  # additive scale path — builder can't emit it yet
        raise IsolationUnavailable(
            "ext4 rootfs boot is not wired yet; build with medium='initramfs'"
        )
    raise IsolationUnavailable(f"unknown rootfs medium {medium!r}")


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
        host_objects: dict[str, Any] | None = None,
        allow: dict[str, set[str]] | None = None,
        cache: dict[str, bytes] | None = None,
        on_emit: Callable[[str, Any], None] | None = None,
    ):
        super().__init__(host_objects, allow, cache, on_emit)
        if platform.system() != "Darwin":
            raise IsolationUnavailable("vfkit rung requires macOS (HVF)")
        home = Path(home) if home else dud_home()
        arch = arch or _host_arch()

        self.build = build_rootfs(image, arch=arch, workspace=workspace, home=home)
        kernel_path = _resolve_kernel(kernel, arch, home)
        vfkit = _vfkit_bin()

        # Short rundir: macOS AF_UNIX sun_path is capped at 104 chars, and
        # $TMPDIR is long, so anchor under /tmp explicitly.
        self._rundir = tempfile.mkdtemp(dir="/tmp", prefix="dud-vm-")
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
            f"console=hvc0 random.trust_cpu=on PYTHONHASHSEED=0 "
            f"dud.mode=connect dud.cid={_HOST_CID} dud.port={_VSOCK_PORT} "
            f"dud.root={workspace}"
        )
        args = [
            vfkit, "--cpus", str(cpus), "--memory", str(memory_mib),
            "--kernel", str(kernel_path),
            "--kernel-cmdline", cmdline,
            *_medium_boot_args(self.build.rootfs_path, self.build.medium),
            "--device", "virtio-rng",
            "--device", f"virtio-serial,logFilePath={self._console}",
            "--device",
            f"virtio-vsock,port={_VSOCK_PORT},socketURL={self._sock_path}",
        ]
        self._vfkit_log = open(os.path.join(self._rundir, "vfkit.log"), "wb")
        self._proc = subprocess.Popen(args, stdout=self._vfkit_log,
                                      stderr=subprocess.STDOUT)

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
            raise IsolationUnavailable(
                f"guest did not connect within {timeout}s ({e}); console tail:\n"
                + self._console_tail()
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
