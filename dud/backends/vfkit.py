"""Rung 2 (macOS): the guest supervisor inside a vfkit microVM.

Same guest, same wire protocol as every other rung — only the VMM
changes. Everything above the hypervisor lives in :mod:`dud.backends.vm`;
this file is the vfkit half and nothing else.

Boot facts settled by the stage-4 spikes (see DESIGN.md):
  - vsock direction is guest->host: the guest dials CID 2
    (``dud.mode=connect``) and the vsock device's ``connect`` qualifier
    makes vfkit forward that to the unix socket the host listens on.
    (vfkit's default is host->guest and drops a guest-initiated dial.)
  - the kernel is a versioned dud asset (arch-matched uncompressed
    ``Image``; see :mod:`dud.kernels`), not shipped by the image. The
    pinned kernel has virtio-rng built in, so entropy is real — the
    old puipui kernel needed a ``PYTHONHASHSEED=0`` cmdline workaround.
  - rootfs medium comes from the build; ``erofs`` attaches as a per-boot
    APFS clone (see :meth:`VfkitSession._medium_boot_args`).

Requesting this rung where it can't run fails closed
(:class:`IsolationUnavailable`) rather than silently degrading.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path

from ..errors import IsolationUnavailable  # noqa: F401 — canonical home is dud.errors
from ..images.scratch import _clone_or_copy
from .vm import VSOCK_PORT, BootSpec, VmSession

# Kept importable here: `sweep_stale_rundirs` was public from this module
# before the rungs shared a base, and it is rung-agnostic either way.
from .vm import sweep_stale_rundirs  # noqa: F401


def _vfkit_bin() -> str:
    exe = shutil.which("vfkit") or "/opt/homebrew/bin/vfkit"
    if not Path(exe).exists():
        raise IsolationUnavailable("vfkit not found (brew install vfkit)")
    return exe


class VfkitSession(VmSession):
    """A workspace session backed by a disposable vfkit microVM."""

    def _preflight(self) -> None:
        if platform.system() != "Darwin":
            raise IsolationUnavailable("vfkit rung requires macOS (HVF)")
        _vfkit_bin()  # fail before any pull/build if the VMM is missing

    def _listen_path(self) -> str:
        return os.path.join(self._rundir, "vsock")

    def _console_arg(self) -> str:
        return "console=hvc0 random.trust_cpu=on"

    def _start_vmm(self, spec: BootSpec) -> subprocess.Popen:
        args = [
            _vfkit_bin(),
            "--cpus", str(spec.cpus), "--memory", str(spec.memory_mib),
            "--kernel", str(spec.kernel),
            "--kernel-cmdline", spec.cmdline,
            *self._medium_boot_args(spec),
            "--device", "virtio-rng",
            "--device", f"virtio-serial,logFilePath={spec.console}",
            # socketURL is a BARE path: vfkit treats a unix:// scheme as
            # part of the path rather than as a scheme.
            "--device",
            f"virtio-vsock,port={VSOCK_PORT},socketURL={spec.listen_path}",
        ]
        # Extra block devices (read-only artifacts: erofs workspace
        # images, published-app snapshots). Guest order: extras follow
        # the rootfs device — /dev/vda.. on initramfs, /dev/vdb.. when
        # the root itself is a block device (erofs).
        for disk in spec.disks:
            args += ["--device", f"virtio-blk,path={disk}"]
        if spec.scratch_clone is not None:
            args += ["--device", f"virtio-blk,path={spec.scratch_clone}"]
        return subprocess.Popen(args, stdout=self._vmm_log,
                                stderr=subprocess.STDOUT)

    @staticmethod
    def _medium_boot_args(spec: BootSpec) -> list[str]:
        """VMM args that provide the rootfs, chosen by its medium."""
        if spec.medium == "initramfs":
            return ["--initrd", str(spec.rootfs)]
        if spec.medium == "erofs":
            # First virtio-blk device: the kernel mounts it as / directly
            # (see vm._medium_cmdline); demand-paged — RAM is pages
            # touched, not image size. Each VM attaches a per-boot APFS
            # clone (instant CoW, zero extra disk): VZ takes an exclusive
            # lock on a read-write attachment, so concurrent VMs can't
            # share one file — and vfkit's virtio-blk exposes no readOnly
            # flag even though the VZ API has one (upstream opportunity;
            # a readonly attach would also restore the cross-VM
            # page-cache sharing the firecracker rung gets for free).
            # The EMPTY initrd is a vfkit-CLI appeasement (its
            # kernel/initrd/cmdline flags are an all-or-nothing group
            # though VZ itself makes initrd optional); the kernel finds
            # no /init in it and falls through to root=.
            from ..images.cpio import FileSet, build_cpio_gz

            clone = Path(spec.rundir) / spec.rootfs.name
            _clone_or_copy(spec.rootfs, clone)
            dummy = Path(spec.rundir) / "empty.cpio.gz"
            dummy.write_bytes(build_cpio_gz(FileSet()))
            return [
                "--initrd", str(dummy),
                "--device", f"virtio-blk,path={clone}",
            ]
        raise IsolationUnavailable(f"unknown rootfs medium {spec.medium!r}")
