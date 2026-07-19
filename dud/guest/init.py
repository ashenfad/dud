"""Guest PID 1: mount the essentials, dial the host over vsock, serve.

The VM rung's entrypoint. The kernel execs ``/init`` (a shebang wrapper,
see ``rootfs._init_script``) as PID 1; that calls :func:`main`. We set up
the minimal mounts a userspace expects, connect a vsock stream back to
the host (or listen, per kernel cmdline), and hand the socket to the same
:class:`~dud.guest.supervisor.Supervisor` the subprocess rung runs — the
guest contract is identical; only the transport differs. When the host
closes the channel we power the VM off so it exits cleanly.

Kernel cmdline knobs (space-separated in ``/proc/cmdline``):
  ``dud.port=N``     vsock port (default 1024)
  ``dud.cid=N``      peer CID to connect to (default 2 = host)
  ``dud.mode=M``     ``connect`` (default) or ``listen``
  ``dud.root=PATH``  workspace root (default from the /init wrapper)
  ``dud.scratch=DEV``  ext4 scratch volume to mount over /tmp (cache
                       plane: best-effort, never boot-blocking)
"""

from __future__ import annotations

import ctypes
import os
import socket
import sys
import time
from pathlib import Path

# vsock well-knowns (Linux uapi). AF_VSOCK may be absent from a non-Linux
# build of this file's host copy, so we hardcode the numbers.
AF_VSOCK = getattr(socket, "AF_VSOCK", 40)
VMADDR_CID_HOST = 2
_RB_POWER_OFF = 0x4321FEDC

_libc = ctypes.CDLL(None, use_errno=True)


def _mount(source: str, target: str, fstype: str, flags: int = 0,
           data: str | None = None) -> None:
    os.makedirs(target, exist_ok=True)
    rc = _libc.mount(
        source.encode(), target.encode(), fstype.encode(),
        ctypes.c_ulong(flags), (data.encode() if data else None),
    )
    if rc != 0:
        err = ctypes.get_errno()
        # EBUSY (16) = already mounted; tolerate it.
        if err != 16:
            _log(f"mount {fstype} {target} failed: errno {err}")


def _mount_essentials() -> None:
    _mount("proc", "/proc", "proc")
    _mount("sysfs", "/sys", "sysfs")
    _mount("devtmpfs", "/dev", "devtmpfs")
    os.makedirs("/dev/pts", exist_ok=True)
    _mount("devpts", "/dev/pts", "devpts")
    _mount("tmpfs", "/run", "tmpfs")
    _mount("tmpfs", "/tmp", "tmpfs")


def _mount_scratch(dev: str) -> None:
    """Mount the scratch volume over /tmp's tmpfs. Best-effort by the
    scratch contract: scratch is cache, so a missing or unmountable
    volume must never stop the machine — the session just runs with a
    RAM-backed /tmp. A volume from a crashed VM mounts clean without
    userspace fsck: ext4 journal replay happens in-kernel at mount."""
    deadline = time.monotonic() + 2.0
    while not os.path.exists(dev) and time.monotonic() < deadline:
        time.sleep(0.05)  # virtio-blk probes async
    if not os.path.exists(dev):
        _log(f"scratch device {dev} never appeared; /tmp stays tmpfs")
        return
    _mount(dev, "/tmp", "ext4")
    try:
        os.chmod("/tmp", 0o1777)  # ext4 root dir is 0755; /tmp is 1777
    except OSError:
        pass


def _log(msg: str) -> None:
    # PID 1 has no logger; write straight to the console.
    try:
        sys.stderr.write(f"[dud-init] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _cmdline() -> dict[str, str]:
    try:
        raw = Path("/proc/cmdline").read_text()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for tok in raw.split():
        if tok.startswith("dud.") and "=" in tok:
            key, _, val = tok.partition("=")
            out[key[len("dud."):]] = val
    return out


def _connect_vsock(cid: int, port: int, deadline: float) -> socket.socket:
    last = None
    while time.monotonic() < deadline:
        s = socket.socket(AF_VSOCK, socket.SOCK_STREAM)
        try:
            s.connect((cid, port))
            return s
        except OSError as e:
            last = e
            s.close()
            time.sleep(0.1)
    raise OSError(f"vsock connect to cid={cid} port={port} failed: {last}")


def _listen_vsock(port: int) -> socket.socket:
    VMADDR_CID_ANY = 0xFFFFFFFF
    srv = socket.socket(AF_VSOCK, socket.SOCK_STREAM)
    srv.bind((VMADDR_CID_ANY, port))
    srv.listen(1)
    conn, _ = srv.accept()
    srv.close()
    return conn


def _power_off() -> None:
    sys.stderr.flush()
    _libc.sync()
    _libc.reboot(_RB_POWER_OFF)


def main(default_root: str = "/workspace") -> None:
    from ..proto import Channel
    from .supervisor import Supervisor

    is_pid1 = os.getpid() == 1
    if is_pid1:
        _mount_essentials()

    opts = _cmdline()
    port = int(opts.get("port", "1024"))
    cid = int(opts.get("cid", str(VMADDR_CID_HOST)))
    mode = opts.get("mode", "connect")
    root = Path(opts.get("root", default_root))
    root.mkdir(parents=True, exist_ok=True)
    if is_pid1 and opts.get("scratch"):
        _mount_scratch(opts["scratch"])

    # Children (the python runner) inherit this so `-m dud.guest.runner`
    # resolves even if the package lives outside the default sys.path.
    site = next((p for p in sys.path if p.endswith("site-packages")), None)
    if site:
        os.environ["PYTHONPATH"] = site + os.pathsep + os.environ.get("PYTHONPATH", "")

    try:
        if mode == "listen":
            _log(f"listening on vsock port {port}")
            sock = _listen_vsock(port)
        else:
            _log(f"connecting to cid={cid} port={port}")
            sock = _connect_vsock(cid, port, time.monotonic() + 10.0)
        _log("channel up")
        channel = Channel(sock)
        supervisor = Supervisor(channel, root)
        channel.hello_send()
        while True:
            reason = channel.serve()
            channel.close()
            # mode=listen has no dial to re-run, so a freeze there
            # falls through to poweroff (after having acked — no live
            # transport uses listen; firecracker/vfkit both connect).
            if reason != "freeze" or mode == "listen":
                break
            # Freeze/thaw (firecracker snapshots): the host acked our
            # freeze response and is about to pause + snapshot us — we
            # may resume milliseconds or days from now, in a VMM the
            # host process spawned fresh. Redial and rebind; all warm
            # state (staging, shell env, template) carries over. The
            # deadline is monotonic, which does not tick while the VM
            # is paused, so it budgets only actual running time: the
            # pre-pause window (dials bounce off a closed listener)
            # plus the post-thaw accept. A host that thaws us and dies
            # before accepting exhausts it and we power off — the
            # no-dangling-VMs invariant, kept.
            _log("frozen; redialing for thaw")
            sock = _connect_vsock(cid, port, time.monotonic() + 60.0)
            _log("thawed; channel up")
            channel = Channel(sock)
            supervisor.rebind(channel)
            channel.hello_send()
        _log("channel closed; powering off")
    except Exception as e:  # noqa: BLE001 — PID 1 must not raise into the kernel
        _log(f"fatal: {type(e).__name__}: {e}")
    finally:
        if is_pid1:
            _power_off()
        else:
            sys.exit(0)


if __name__ == "__main__":
    main()
