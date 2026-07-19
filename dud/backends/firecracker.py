"""Rung 3 (Linux/KVM): the guest supervisor inside a Firecracker microVM.

Same guest, same wire protocol, same conformance corpus as rungs 1-2 —
only the transport changes (the ladder's whole invariant). Firecracker
is configured over its HTTP-over-unix-socket API (machine-config,
boot-source, drives, vsock, InstanceStart); the guest's
``dud.guest.init`` dials CID 2 as always, which Firecracker forwards
to a host unix socket at ``<uds>_<port>``.

Deltas from the vfkit transport, all simplifications:
  - erofs roots attach with ``is_read_only`` — no per-boot clone, and
    concurrent VMs of one image share the host page cache (the thing
    vfkit's missing readOnly flag cost us).
  - no empty-initrd appeasement: kernel/initrd/cmdline are independent
    API fields, so a block root just omits the initrd.
  - extra ``disks=`` attach read-only too (they are read-only
    artifacts by contract; vfkit could only enforce that by cloning).

The scratch volume keeps its per-boot clone (it is writable by
design); ``_clone_or_copy`` reflinks where the host fs can.

Requesting this rung where it can't run fails closed
(:class:`IsolationUnavailable`): Linux + /dev/kvm + a firecracker
binary (``$DUD_FIRECRACKER`` or on PATH).
"""

from __future__ import annotations

import http.client
import json
import os
import platform
import shutil
import socket as socketlib
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from ..errors import IsolationUnavailable
from ..images import build as build_rootfs, dud_home
from ..images.scratch import _clone_or_copy, promote_clone
from ..proto import Channel
from .base import HostSession
from .vfkit import (
    _RUNDIR_PREFIX,
    _host_arch,
    _medium_cmdline,
    _resolve_kernel,
    _scratch_device,
    _sweep_once,
)

_VSOCK_PORT = 1024
_GUEST_CID = 3  # any CID > 2; the guest still dials CID 2 (the host)


def _fc_bin() -> str:
    exe = os.environ.get("DUD_FIRECRACKER") or shutil.which("firecracker")
    if not exe or not Path(exe).exists():
        raise IsolationUnavailable(
            "firecracker not found (put it on PATH or set $DUD_FIRECRACKER)"
        )
    return exe


class _UnixHTTPConnection(http.client.HTTPConnection):
    """http.client over an AF_UNIX socket (firecracker's API plane)."""

    def __init__(self, path: str, timeout: float = 5.0):
        super().__init__("localhost", timeout=timeout)
        self._unix_path = path

    def connect(self) -> None:
        s = socketlib.socket(socketlib.AF_UNIX, socketlib.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._unix_path)
        self.sock = s


class FirecrackerSession(HostSession):
    """A workspace session backed by a disposable Firecracker microVM."""

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
        medium: str = "auto",
        scratch: str | Path | None = None,
        host_objects: dict[str, Any] | None = None,
        allow: dict[str, set[str]] | None = None,
        cache: dict[str, bytes] | None = None,
        on_emit: Callable[[str, Any], None] | None = None,
    ):
        super().__init__(host_objects, allow, cache, on_emit)
        if platform.system() != "Linux":
            raise IsolationUnavailable("firecracker rung requires Linux/KVM")
        if not os.access("/dev/kvm", os.R_OK | os.W_OK):
            raise IsolationUnavailable(
                "/dev/kvm is not accessible (missing, or not in the kvm group)"
            )
        for disk in disks or []:
            if not Path(disk).is_file():
                raise IsolationUnavailable(f"disk image not found: {disk}")
        if scratch is not None and not Path(scratch).is_file():
            raise IsolationUnavailable(f"scratch volume not found: {scratch}")
        fc = _fc_bin()

        # Pooling hooks: interface parity with VfkitSession. The shared
        # pool is vfkit-typed today; firecracker pooling arrives with
        # the snapshot/restore work, where parking becomes a file.
        self._pool: Any = None
        self.park_state: str | None = None
        self.resumed = False
        self._pool_kwargs = {
            "image": image, "arch": arch, "workspace": workspace,
            "kernel": kernel, "memory_mib": memory_mib, "cpus": cpus,
            "home": home, "packages": packages, "debs": debs,
            "disks": [str(d) for d in disks] if disks else None,
            "medium": medium,
            "scratch": str(scratch) if scratch else None,
        }
        home = Path(home) if home else dud_home()
        arch = arch or _host_arch()

        self.build = build_rootfs(
            image, arch=arch, workspace=workspace, home=home,
            packages=packages, debs=debs, medium=medium,
        )
        kernel_path = _resolve_kernel(kernel, arch, home)

        _sweep_once()
        # /tmp anchoring is inherited from vfkit (macOS sun_path cap)
        # and kept for sweep symmetry. Known tradeoff: on distros where
        # /tmp is tmpfs (Fedora/Arch), the writable scratch clone lives
        # in RAM for the VM's lifetime — revisit if those become
        # deployment targets (validated targets: Ubuntu, ubuntu-latest).
        self._rundir = tempfile.mkdtemp(dir="/tmp", prefix=_RUNDIR_PREFIX)
        self._api_sock = os.path.join(self._rundir, "fc.sock")
        self._vsock_uds = os.path.join(self._rundir, "vsock")
        self._console = os.path.join(self._rundir, "console.log")

        self._scratch_master = Path(scratch) if scratch else None
        self._scratch_clone: Path | None = None
        if self._scratch_master is not None:
            self._scratch_clone = Path(self._rundir) / "scratch.img"
            _clone_or_copy(self._scratch_master, self._scratch_clone)

        # Guest-initiated vsock connections to port P land on the unix
        # socket at "<uds>_<P>" — listen before boot so the guest's
        # early dial has a peer (same discipline as the vfkit rung).
        self._srv = socketlib.socket(socketlib.AF_UNIX, socketlib.SOCK_STREAM)
        self._srv.bind(f"{self._vsock_uds}_{_VSOCK_PORT}")
        self._srv.listen(1)

        # Console (serial) rides the firecracker process's stdout.
        self._fc_log = open(self._console, "wb")
        self._proc = subprocess.Popen(
            [fc, "--api-sock", self._api_sock],
            stdout=self._fc_log, stderr=subprocess.STDOUT,
        )
        Path(self._rundir, "pid").write_text(str(self._proc.pid))

        try:
            self._configure(kernel_path, workspace, cpus, memory_mib,
                            disks or [])
            conn = self._accept(boot_timeout)
        except Exception as e:
            self._teardown_vm()
            tail = self._console_tail()  # empty for pre-InstanceStart failures
            try:
                self._srv.close()
            except OSError:
                pass
            shutil.rmtree(self._rundir, ignore_errors=True)
            raise IsolationUnavailable(
                f"firecracker boot failed ({e}); console tail:\n{tail}"
            ) from e
        self._ch = Channel(conn, handler=self._handle)
        self._ch.hello_recv()

    # ---- firecracker API plane ----------------------------------------

    def _api(self, method: str, resource: str, body: dict | None = None) -> None:
        conn = _UnixHTTPConnection(self._api_sock)
        try:
            conn.request(method, resource,
                         body=json.dumps(body) if body is not None else None,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            data = resp.read()
            if resp.status >= 400:
                raise IsolationUnavailable(
                    f"firecracker API {method} {resource} -> {resp.status}: "
                    f"{data.decode(errors='replace')}"
                )
        finally:
            conn.close()

    def _await_api(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                s = socketlib.socket(socketlib.AF_UNIX, socketlib.SOCK_STREAM)
                s.connect(self._api_sock)
                s.close()
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)

    def _configure(self, kernel_path: Path, workspace: str, cpus: int,
                   memory_mib: int, disks: list) -> None:
        self._await_api()
        self._api("PUT", "/machine-config",
                  {"vcpu_count": cpus, "mem_size_mib": memory_mib,
                   "smt": False})
        cmdline = (
            f"console=ttyS0 reboot=k panic=-1 "
            f"dud.mode=connect dud.cid=2 dud.port={_VSOCK_PORT} "
            f"dud.root={workspace}"
        ) + _medium_cmdline(self.build.medium)
        if self._scratch_clone is not None:
            cmdline += (
                f" dud.scratch="
                f"{_scratch_device(self.build.medium, len(disks))}"
            )
        boot: dict[str, Any] = {
            "kernel_image_path": str(kernel_path),
            "boot_args": cmdline,
        }
        if self.build.medium == "initramfs":
            boot["initrd_path"] = str(self.build.rootfs_path)
        self._api("PUT", "/boot-source", boot)
        if self.build.medium == "erofs":
            # Read-only attach: no clone, and N VMs of one image share
            # the host page cache — structurally what the medium wants.
            self._api("PUT", "/drives/rootfs", {
                "drive_id": "rootfs", "is_root_device": True,
                "is_read_only": True,
                "path_on_host": str(self.build.rootfs_path),
            })
        for i, disk in enumerate(disks):
            self._api("PUT", f"/drives/disk{i}", {
                "drive_id": f"disk{i}", "is_root_device": False,
                "is_read_only": True, "path_on_host": str(Path(disk)),
            })
        if self._scratch_clone is not None:
            self._api("PUT", "/drives/scratch", {
                "drive_id": "scratch", "is_root_device": False,
                "is_read_only": False,
                "path_on_host": str(self._scratch_clone),
            })
        self._api("PUT", "/vsock",
                  {"guest_cid": _GUEST_CID, "uds_path": self._vsock_uds})
        try:
            # virtio-rng (firecracker >= 1.0). Best-effort: the pinned
            # kernel also carries jitter entropy, so absence degrades
            # to slower first-boot entropy, not to a hang.
            self._api("PUT", "/entropy", {})
        except IsolationUnavailable:
            pass
        self._api("PUT", "/actions", {"action_type": "InstanceStart"})

    # ---- boot / teardown ------------------------------------------------

    def _accept(self, timeout: float) -> socketlib.socket:
        self._srv.settimeout(timeout)
        conn, _ = self._srv.accept()
        return conn

    def _console_tail(self, n: int = 25) -> str:
        try:
            lines = Path(self._console).read_text(errors="replace").splitlines()
            return "\n".join(lines[-n:])
        except OSError:
            return "(no console output)"

    def _teardown_vm(self) -> None:
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        try:
            self._fc_log.close()
        except Exception:
            pass

    # ---- scratch ---------------------------------------------------------

    def promote_scratch(self) -> None:
        """See DESIGN.md "The scratch plane": last CLEAN park/shutdown
        wins; crashed clones die with the rundir."""
        if self._scratch_master is None or self._scratch_clone is None:
            return
        promote_clone(self._scratch_master, self._scratch_clone,
                      tag=f"{id(self):x}")

    def close(self, park_state: str | None = None) -> None:
        if park_state is not None:
            self.park_state = park_state
        if self._closed:
            return
        self._closed = True
        if self._pool is not None:  # future: snapshot-backed parking
            self._pool.release(self)
            return
        clean = False
        try:
            self._ch.request("shutdown")
            clean = True
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
            clean = False
        if clean:
            try:
                self.promote_scratch()
            except OSError:
                pass
        for closeable in (self._srv, self._fc_log):
            try:
                closeable.close()
            except Exception:
                pass
        shutil.rmtree(self._rundir, ignore_errors=True)
