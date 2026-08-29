"""Rung 3 (Linux/KVM): the guest supervisor inside a Firecracker microVM.

Same guest, same wire protocol, same conformance corpus as rungs 1-2 —
only the VMM changes (the ladder's whole invariant). Everything above
the hypervisor lives in :mod:`dud.backends.vm`; this file owns the
Firecracker API plane and snapshot parking.

Firecracker is configured over HTTP-on-a-unix-socket (machine-config,
boot-source, drives, vsock, InstanceStart); the guest's
``dud.guest.init`` dials CID 2 as always, which Firecracker forwards to
a host unix socket at ``<uds>_<port>``.

Deltas from the vfkit transport, all simplifications:
  - erofs roots attach with ``is_read_only`` — no per-boot clone, and
    concurrent VMs of one image share the host page cache (the thing
    vfkit's missing readOnly flag costs).
  - no empty-initrd appeasement: kernel/initrd/cmdline are independent
    API fields, so a block root just omits the initrd.
  - extra ``disks=`` attach read-only too (they are read-only artifacts
    by contract; vfkit could only enforce that by cloning).

The scratch volume keeps its per-boot clone (it is writable by design);
the shared base reflinks it where the host fs can.

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
import time
from pathlib import Path
from typing import Any

from ..errors import IsolationUnavailable
from ..proto import Channel
from .vm import VSOCK_PORT, BootSpec, VmSession, _write_marker

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


class FirecrackerSession(VmSession):
    """A workspace session backed by a disposable Firecracker microVM."""

    def _preflight(self) -> None:
        if platform.system() != "Linux":
            raise IsolationUnavailable("firecracker rung requires Linux/KVM")
        if not os.access("/dev/kvm", os.R_OK | os.W_OK):
            raise IsolationUnavailable(
                "/dev/kvm is not accessible (missing, or not in the kvm group)"
            )
        self._fc_exe = _fc_bin()

    def _listen_path(self) -> str:
        # Guest-initiated vsock connections to port P land on the unix
        # socket at "<uds>_<P>".
        self._vsock_uds = os.path.join(self._rundir, "vsock")
        return f"{self._vsock_uds}_{VSOCK_PORT}"

    def _console_arg(self) -> str:
        return "console=ttyS0 reboot=k panic=-1"

    def _vmm_log_path(self, spec: BootSpec) -> str:
        # The guest's serial console rides firecracker's own stdout, so
        # the VMM log and the console are one file here.
        return spec.console

    def _start_vmm(self, spec: BootSpec) -> subprocess.Popen:
        self._api_sock = os.path.join(spec.rundir, "fc.sock")
        proc = subprocess.Popen(
            [self._fc_exe, "--api-sock", self._api_sock],
            stdout=self._vmm_log, stderr=subprocess.STDOUT,
        )
        self._configure(spec)
        return proc

    # ---- firecracker API plane ----------------------------------------

    def _api(self, method: str, resource: str, body: dict | None = None,
             timeout: float = 5.0) -> None:
        conn = _UnixHTTPConnection(self._api_sock, timeout=timeout)
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

    def _configure(self, spec: BootSpec) -> None:
        self._await_api()
        self._api("PUT", "/machine-config",
                  {"vcpu_count": spec.cpus, "mem_size_mib": spec.memory_mib,
                   "smt": False})
        boot: dict[str, Any] = {
            "kernel_image_path": str(spec.kernel),
            "boot_args": spec.cmdline,
        }
        if spec.medium == "initramfs":
            boot["initrd_path"] = str(spec.rootfs)
        self._api("PUT", "/boot-source", boot)
        if spec.medium == "erofs":
            # Read-only attach: no clone, and N VMs of one image share
            # the host page cache — structurally what the medium wants.
            self._api("PUT", "/drives/rootfs", {
                "drive_id": "rootfs", "is_root_device": True,
                "is_read_only": True,
                "path_on_host": str(spec.rootfs),
            })
        for i, disk in enumerate(spec.disks):
            self._api("PUT", f"/drives/disk{i}", {
                "drive_id": f"disk{i}", "is_root_device": False,
                "is_read_only": True, "path_on_host": str(disk),
            })
        if spec.scratch_clone is not None:
            self._api("PUT", "/drives/scratch", {
                "drive_id": "scratch", "is_root_device": False,
                "is_read_only": False,
                "path_on_host": str(spec.scratch_clone),
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

    # ---- freeze / thaw ---------------------------------------------------

    def freeze(self) -> None:
        """Park this VM as files: snapshot memory + device state into
        the rundir and kill the VMM. A frozen session costs zero RAM
        and zero CPU; :meth:`thaw` resumes it in tens of milliseconds
        with all guest state — filesystem, shell env, live memory —
        exactly where it was.

        The guest cooperates via the ``freeze`` verb (it syncs, acks,
        closes the channel, and enters a bounded redial loop), so a
        bare EOF keeps meaning "die" on every other path. The rundir
        must survive as-is: the snapshot's device table references the
        disk files (rootfs, debs, scratch clone) by absolute path. A
        ``frozen`` marker carrying our pid keeps the sweep off it for
        exactly as long as this process lives."""
        if self.frozen:
            return
        if self._closed and self._pool is None:
            raise RuntimeError("cannot freeze a closed session")
        # Close the listener before the freeze verb: the guest starts
        # redialing the moment it acks, and those dials must bounce
        # rather than land on the pre-freeze listener.
        try:
            self._srv.close()
        except OSError:
            pass
        self._request("freeze")
        self._ch.close()
        # A paused guest can never see channel EOF, so if we die
        # between Pause and the VMM kill the process-linkage cascade is
        # dead and the VMM would dangle forever. The freezing marker
        # (host pid + VMM pid) lets any later sweep finish the job:
        # owner dead -> kill the recorded VMM if it still serves this
        # rundir, then reap the bundle.
        _write_marker(Path(self._rundir, "freezing"),
                      f"{os.getpid()} {self._proc.pid}")
        self._api("PATCH", "/vm", {"state": "Paused"})
        for name in ("vmstate", "mem"):
            try:
                os.unlink(os.path.join(self._rundir, name))
            except OSError:
                pass
        # snapshot/create answers only after writing the FULL guest
        # memory file — size the timeout to RAM at worst-case ~25 MB/s
        # (loaded disks, cloud block storage), never the 5s default.
        mem_mib = int(self._pool_kwargs.get("memory_mib") or 2048)
        self._api("PUT", "/snapshot/create", {
            "snapshot_type": "Full",
            "snapshot_path": os.path.join(self._rundir, "vmstate"),
            "mem_file_path": os.path.join(self._rundir, "mem"),
        }, timeout=max(60.0, mem_mib / 25.0))
        # Marker order matters against a concurrent sweep: publish
        # `frozen` (atomically — a torn read must not look like a
        # garbage marker) BEFORE the VMM dies, so there is no instant
        # where the rundir shows only a dead pidfile.
        _write_marker(Path(self._rundir, "frozen"), str(os.getpid()))
        self._teardown_vm()
        try:
            os.unlink(os.path.join(self._rundir, "freezing"))
        except OSError:
            pass
        self.frozen = True

    def thaw(self, timeout: float = 30.0) -> None:
        """Resume a frozen session in a fresh VMM. Fast path: the
        memory file is mmap'd, not read — resume latency is near
        constant in guest RAM size, and pages fault in on demand.

        After the guest redials we send ``resync``: the wall clock
        stopped at snapshot time, and the fork template pre-dates the
        snapshot (identical PRNG state across clones of one snapshot),
        so the guest sets the clock and re-warms the template."""
        if not self.frozen:
            return
        # The dead VMM's socket files linger; both would EADDRINUSE
        # the new process (firecracker refuses an existing API socket,
        # and re-creates the vsock listener from the snapshot config).
        for stale in (self._api_sock, self._vsock_uds, self._sock_path):
            try:
                os.unlink(stale)
            except OSError:
                pass
        self._srv = socketlib.socket(socketlib.AF_UNIX, socketlib.SOCK_STREAM)
        self._srv.bind(self._sock_path)
        self._srv.listen(1)
        self._vmm_log = open(self._console, "ab")
        self._proc = subprocess.Popen(
            [self._fc_exe, "--api-sock", self._api_sock],
            stdout=self._vmm_log, stderr=subprocess.STDOUT,
        )
        Path(self._rundir, "pid").write_text(str(self._proc.pid))
        try:
            self._await_api()
            self._api("PUT", "/snapshot/load", {
                "snapshot_path": os.path.join(self._rundir, "vmstate"),
                "mem_backend": {
                    "backend_type": "File",
                    "backend_path": os.path.join(self._rundir, "mem"),
                },
                "resume_vm": True,
            })
            conn = self._accept(timeout)
        except Exception as e:
            self._teardown_vm()
            try:
                self._srv.close()
            except OSError:
                pass
            tail = self._console_tail()
            raise IsolationUnavailable(
                f"firecracker thaw failed ({e}); console tail:\n{tail}"
            ) from e
        self._ch = Channel(conn, handler=self._handle)
        self._ch.hello_recv()
        self.frozen = False
        try:
            os.unlink(os.path.join(self._rundir, "frozen"))
        except OSError:
            pass
        self._request("resync", {"epoch": time.time()})
