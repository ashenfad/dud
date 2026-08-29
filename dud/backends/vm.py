"""The VM rungs' shared half: everything above the hypervisor.

Rungs 2 and 3 differ in one thing — which VMM boots the guest and how
it is told to. Everything else is identical by design, because that
identity *is* the ladder's invariant: same guest supervisor, same
rootfs, same wire protocol, same conformance corpus, only the substrate
hardens. This module is where that shared half actually lives, so the
invariant is expressed in code rather than maintained by hand.

It did not start here. ``vfkit.py`` was the de-facto base for a while:
``firecracker.py`` imported six private names from it, ``pool.py``
annotated everything ``VfkitSession`` while duck-typing the rest, and
``sweep_stale_rundirs`` — which reaps *firecracker snapshot bundles* —
lived in the macOS backend. That arrangement worked and said something
false, namely that rung 3 is a special case of rung 2.

A subclass provides:

- ``_preflight()`` — refuse this rung where it cannot run
  (``IsolationUnavailable``), before any pull or build is paid for
- ``_listen_path()`` — where the host listens for the guest's dial
- ``_start_vmm(spec)`` — boot the machine; return the VMM ``Popen``

and, where the substrate can do it, ``freeze`` / ``thaw``. The pool
duck-types those two: a backend that has them parks as files, one that
doesn't parks hot, and neither knows about the other.

DESIGN.md says the backend seam "should stay honest enough that a
remote driver is a plausible later rung." This is that seam: a driver
that rents a machine implements the three hooks and inherits the
rundir, scratch, sweep, pooling and teardown contracts unchanged.
"""

from __future__ import annotations

import os
import platform
import shutil
import signal
import socket as socketlib
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..errors import IsolationUnavailable
from ..images import build as build_rootfs, dud_home
from ..images.scratch import _clone_or_copy, promote_clone
from ..proto import Channel
from .base import HostSession

VSOCK_PORT = 1024
HOST_CID = 2

_RUNDIR_PREFIX = "dud-vm-"
_swept = False


# ---- host facts --------------------------------------------------------


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


def _scratch_device(medium: str, n_disks: int) -> str:
    """Guest name of the scratch volume: it is attached last, after
    the rootfs block device (erofs only) and any extra disks."""
    return "/dev/vd" + "abcdefghij"[(1 if medium == "erofs" else 0) + n_disks]


def _medium_cmdline(medium: str) -> str:
    """Extra kernel cmdline for the medium (appended to the dud.* set)."""
    if medium == "erofs":
        # rootwait: virtio-blk probes async; don't panic before /dev/vda.
        # init=/init: on a real root the kernel would look for
        # /sbin/init — our entrypoint keeps its initramfs name.
        return " root=/dev/vda rootfstype=erofs ro rootwait init=/init"
    return ""


# ---- stale rundir sweep ------------------------------------------------


def _vmm_alive(pid: int, rundir: str) -> bool:
    """Is ``pid`` a live VMM serving ``rundir``? The command-line check
    guards against pid reuse: every VMM invocation carries its rundir in
    its args (socket/console paths)."""
    try:
        # -ww: unlimited width. procps (Linux) otherwise truncates to
        # $COLUMNS even when piped (pytest exports COLUMNS=80), which
        # cut argv before the rundir and made live VMs look stale.
        out = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True,
        )
    except OSError:
        return False
    return rundir in out.stdout


def sweep_stale_rundirs(root: str | Path = "/tmp") -> list[str]:
    """Remove rundirs (sockets, logs, disk clones, snapshot bundles)
    orphaned by a host that died hard.

    Processes can't dangle — channel EOF powers the guest off and the
    VMM exits with it — but their on-disk rundirs can. A dir whose
    recorded VMM pid is live is someone else's running VM and is left
    alone; one with no pidfile is only removed once it's old enough
    (10 min) to rule out a concurrent mid-boot.

    Rung-agnostic on purpose: the markers it honors are written by
    whichever backend can produce them (only firecracker freezes
    today), and a sweep that only understood the rung it was filed
    under would leave the other rung's garbage behind.
    """
    removed: list[str] = []
    for path in Path(root).glob(_RUNDIR_PREFIX + "*"):
        # Freeze-in-progress: between Pause and the VMM kill, the
        # process-linkage cascade is disarmed — a paused guest can never
        # see channel EOF, so a host that dies in that window leaves a
        # VMM that will NEVER exit on its own. The marker records "host
        # pid, VMM pid": while the host lives the freeze is someone's
        # work in progress; once it's dead, kill the recorded VMM
        # (argv-checked against this rundir to guard pid reuse) and reap
        # the bundle.
        freezing = path / "freezing"
        if freezing.exists():
            owner = vmm = None
            try:
                owner_s, vmm_s = freezing.read_text().split()
                owner, vmm = int(owner_s), int(vmm_s)
            except (OSError, ValueError):
                pass
            if owner is not None:
                try:
                    os.kill(owner, 0)
                    continue  # live owner: freeze in progress
                except ProcessLookupError:
                    pass
                except (PermissionError, OSError):
                    continue  # alive but not ours: leave it alone
                if vmm is not None and _vmm_alive(vmm, str(path)):
                    try:
                        os.kill(vmm, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
            shutil.rmtree(path, ignore_errors=True)
            removed.append(str(path))
            continue
        # Frozen park (snapshots): no VMM is running, the rundir IS the
        # VM — vmstate + memory + disk clones. The marker records the
        # owning host process; while that pid lives the bundle is
        # somebody's parked session, and once it dies the bundle is
        # garbage (a snapshot nobody can thaw).
        frozen = path / "frozen"
        if frozen.exists():
            try:
                owner = int(frozen.read_text())
            except (OSError, ValueError):
                owner = None
            if owner is not None:
                try:
                    os.kill(owner, 0)
                    continue  # live owner: leave the frozen VM alone
                except ProcessLookupError:
                    pass  # owner died; fall through to removal
                except (PermissionError, OSError):
                    continue  # alive but not ours: leave it alone
            shutil.rmtree(path, ignore_errors=True)
            removed.append(str(path))
            continue
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
            if _vmm_alive(pid, str(path)):
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


def _write_marker(path: Path, text: str) -> None:
    """Atomic marker write (tmp + rename): a concurrent sweep must see
    the old content or the new content, never a torn/empty file (which
    reads as a garbage marker and gets the bundle reaped)."""
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(text)
    os.replace(tmp, path)


# ---- boot spec ---------------------------------------------------------


@dataclass
class BootSpec:
    """Everything a VMM needs, resolved and validated by the base.

    A parameter object rather than eight arguments because it is also
    the checklist for a new rung: whatever a driver needs to boot a
    machine is here, and anything it reaches past this for is a sign
    the seam is in the wrong place.
    """

    kernel: Path
    cmdline: str
    rootfs: Path
    medium: str
    disks: list[Path]
    scratch_clone: Path | None
    rundir: str
    console: str
    listen_path: str
    cpus: int
    memory_mib: int


class VmSession(HostSession):
    """A workspace session backed by a disposable microVM.

    Owns the rundir, the scratch clone, the listener, the boot
    handshake, pooling hooks and teardown. Subclasses own the VMM.
    """

    #: Console lines shown when a boot fails. The VMM writes them.
    _CONSOLE_TAIL_LINES = 25

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
        self._preflight()
        for disk in disks or []:
            # Validate up front: fail before any pull/build work is spent.
            if not Path(disk).is_file():
                raise IsolationUnavailable(f"disk image not found: {disk}")
        if scratch is not None and not Path(scratch).is_file():
            raise IsolationUnavailable(f"scratch volume not found: {scratch}")

        # Pooling hooks (see backends/pool.py): when a pool owns this VM,
        # close() parks it there instead of powering off; _pool_kwargs is
        # the boot fingerprint source. park_state (stamped by the owner
        # before close) tags the parked tree's content identity;
        # resumed=True on acquire means the tree already matches and the
        # owner may skip its push.
        self._pool: Any = None
        self.park_state: str | None = None
        self.resumed = False
        self.frozen = False
        self._pool_kwargs = {
            "image": image, "arch": arch, "workspace": workspace,
            "kernel": kernel, "memory_mib": memory_mib, "cpus": cpus,
            "home": home, "packages": packages, "debs": debs,
            "disks": [str(d) for d in disks] if disks else None,
            "medium": medium,
            # Scratch is boot identity on purpose: a pooled VM may only
            # serve sessions keyed to the SAME master (no cross-key
            # cache leakage through reuse).
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
        # Short rundir: macOS AF_UNIX sun_path is capped at 104 chars, and
        # $TMPDIR is long, so anchor under /tmp explicitly. Kept on Linux
        # for sweep symmetry — known tradeoff: where /tmp is a tmpfs
        # (Fedora/Arch), a writable scratch clone lives in RAM for the
        # VM's lifetime (validated targets: Ubuntu, ubuntu-latest).
        self._rundir = tempfile.mkdtemp(dir="/tmp", prefix=_RUNDIR_PREFIX)
        self._console = os.path.join(self._rundir, "console.log")

        # Scratch volume: a per-boot clone of the caller's master, since
        # it is writable by design and VMs must not share one file. The
        # clone IS the persisted artifact — promotion back to the master
        # is a reflink+rename on clean park/shutdown.
        self._scratch_master = Path(scratch) if scratch else None
        self._scratch_clone: Path | None = None
        if self._scratch_master is not None:
            self._scratch_clone = Path(self._rundir) / "scratch.img"
            _clone_or_copy(self._scratch_master, self._scratch_clone)

        # Listen before boot so the guest's early dial has a peer. vsock
        # direction is guest->host on every rung: the guest dials CID 2
        # and the VMM bridges that to this unix socket.
        self._sock_path = self._listen_path()
        self._srv = socketlib.socket(socketlib.AF_UNIX, socketlib.SOCK_STREAM)
        self._srv.bind(self._sock_path)
        self._srv.listen(1)

        spec = BootSpec(
            kernel=kernel_path,
            cmdline=self._cmdline(workspace, len(disks or [])),
            rootfs=self.build.rootfs_path,
            medium=self.build.medium,
            disks=[Path(d) for d in disks or []],
            scratch_clone=self._scratch_clone,
            rundir=self._rundir,
            console=self._console,
            listen_path=self._sock_path,
            cpus=cpus,
            memory_mib=memory_mib,
        )
        self._vmm_log = open(self._vmm_log_path(spec), "wb")
        try:
            self._proc = self._start_vmm(spec)
            # Liveness record for sweep_stale_rundirs (a future host
            # process cleaning up after a crash of THIS one).
            Path(self._rundir, "pid").write_text(str(self._proc.pid))
            conn = self._accept(boot_timeout)
        except Exception as e:
            tail = self._abandon()
            if isinstance(e, IsolationUnavailable):
                raise
            raise IsolationUnavailable(
                f"{type(self).__name__} boot failed ({e}); console tail:\n{tail}"
            ) from e
        self._ch = Channel(conn, handler=self._handle)
        self._ch.hello_recv()

    # ---- subclass hooks -------------------------------------------------

    def _preflight(self) -> None:
        """Refuse this rung where it can't run. Called before any pull or
        build, so an unavailable rung costs nothing but the raise."""
        raise NotImplementedError

    def _listen_path(self) -> str:
        """Host-side unix socket the guest's vsock dial is bridged to."""
        raise NotImplementedError

    def _start_vmm(self, spec: BootSpec) -> subprocess.Popen:
        """Boot the machine. Return the VMM process."""
        raise NotImplementedError

    def _cmdline(self, workspace: str, n_disks: int) -> str:
        """Kernel cmdline. The ``dud.*`` knobs are the guest contract
        (see :mod:`dud.guest.init`); the console argument is the VMM's
        business, which is why the subclass supplies its prefix."""
        cmdline = (
            f"{self._console_arg()} "
            f"dud.mode=connect dud.cid={HOST_CID} dud.port={VSOCK_PORT} "
            f"dud.root={workspace}"
        ) + _medium_cmdline(self.build.medium)
        if self._scratch_clone is not None:
            cmdline += (
                f" dud.scratch={_scratch_device(self.build.medium, n_disks)}"
            )
        return cmdline

    def _console_arg(self) -> str:
        raise NotImplementedError

    def _vmm_log_path(self, spec: BootSpec) -> str:
        """Where the VMM's own stdout/stderr goes.

        Its own file by default, because a VMM that logs to stdout and a
        guest console that arrives by another route are two different
        streams. A rung whose guest console *is* the VMM's stdout
        (firecracker's serial) points this at the console instead, so
        the two don't fight over one fd.
        """
        return os.path.join(spec.rundir, "vmm.log")

    # ---- boot / teardown -------------------------------------------------

    def _accept(self, timeout: float) -> socketlib.socket:
        """Wait for the VMM to bridge the guest's outbound connection."""
        self._srv.settimeout(timeout)
        conn, _ = self._srv.accept()
        return conn

    def _console_tail(self, n: int | None = None) -> str:
        try:
            lines = Path(self._console).read_text(errors="replace").splitlines()
            return "\n".join(lines[-(n or self._CONSOLE_TAIL_LINES):])
        except OSError:
            return "(no console output)"

    def _abandon(self) -> str:
        """Give up on a half-built session; return the console tail.

        Only for the constructor's failure path, where there is no
        channel to shut down and nothing worth promoting.

        The order is the whole reason this is one method. The VMM must
        die *before* the tail is read: on a rung whose guest console
        rides the VMM's own stdout (firecracker's serial), the last
        lines sit in that file's buffer until the process is reaped and
        the log closed — and those lines are exactly what a failed boot
        is being asked about. The tail must then be read *before* the
        rundir is removed, since that is where the console file lives.
        Getting either half backwards yields "(no console output)" on
        precisely the failures that need explaining.
        """
        self._teardown_vm()
        tail = self._console_tail()
        for closeable in (getattr(self, "_srv", None),
                          getattr(self, "_vmm_log", None)):
            try:
                if closeable is not None:
                    closeable.close()
            except OSError:
                pass
        shutil.rmtree(self._rundir, ignore_errors=True)
        return tail

    def _teardown_vm(self) -> None:
        proc = getattr(self, "_proc", None)
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        try:
            self._vmm_log.close()
        except (OSError, AttributeError):
            pass

    # ---- scratch ---------------------------------------------------------

    def promote_scratch(self) -> None:
        """Publish this VM's scratch clone as the new master.

        Cache semantics: last CLEAN park/shutdown wins; a crashed VM's
        clone is never promoted (it dies with the rundir — losing a
        cache is an inconvenience, not an error). Callers ensure the
        guest has synced first (``reset_guest`` syncs; kernel poweroff
        syncs); the ext4 journal covers the copy being taken of a
        still-mounted volume.
        """
        if self._scratch_master is None or self._scratch_clone is None:
            return
        promote_clone(self._scratch_master, self._scratch_clone,
                      tag=f"{id(self):x}")

    # ---- close -----------------------------------------------------------

    def close(self, park_state: str | None = None) -> None:
        """Close the session. Pooled: parks the VM (``park_state`` tags
        the tree's content identity for a same-state resume — equivalent
        to stamping ``self.park_state`` before closing). Unpooled:
        graceful poweroff."""
        if park_state is not None:
            self.park_state = park_state
        if self._closed:
            return
        self._closed = True
        if self._pool is not None:
            # Pooled: park for the next session (the pool resets the
            # guest; a failed reset tears the VM down).
            self._pool.release(self)
            return
        if self.frozen:
            # Discarding a frozen park is a disposal path: the guest
            # never gets a clean shutdown, so no scratch promotion —
            # the snapshot dies with its rundir.
            try:
                self._vmm_log.close()
            except OSError:
                pass
            shutil.rmtree(self._rundir, ignore_errors=True)
            return
        # shutdown verb -> supervisor stops serving -> init powers off.
        clean = False
        try:
            self._request("shutdown")
            clean = True
        except Exception:  # noqa: BLE001 — a guest mid-death answers anything
            pass
        try:
            self._ch.close()
        except OSError:
            pass
        try:
            self._proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self._teardown_vm()
            clean = False
        if clean:
            # Graceful poweroff: the kernel synced the scratch volume on
            # the way down, so the clone is promotable.
            try:
                self.promote_scratch()
            except OSError:
                pass
        for closeable in (self._srv, self._vmm_log):
            try:
                closeable.close()
            except OSError:
                pass
        shutil.rmtree(self._rundir, ignore_errors=True)
