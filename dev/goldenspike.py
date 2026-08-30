"""SPIKE: can one snapshot be restored into several concurrent VMs?

This is the load-bearing question under golden snapshots. The plan is
to freeze ONE clean VM per boot fingerprint and have every session
thaw a clone of it and then discard it — no per-session freeze (~3s
for a 1 GiB guest), no reset hygiene, and every session gets a
pristine machine rather than a reused one.

dud's own `thaw()` cannot answer it: it resumes the session that froze
itself, reusing that session's rundir, API socket and vsock UDS. A
clone needs its own of each, while sharing the golden memory file.
Two things make that possible, both checked against the v1.16.1 API
spec before writing this:

  - `vsock_override.uds_path` on /snapshot/load, whose description is
    literally about restoring one snapshot into multiple VMs;
  - `mem_backend` File, which firecracker maps MAP_PRIVATE — so
    clones share the golden file copy-on-write and none of them can
    write through to it.

What this proves or disproves, deliberately end-to-end: that N clones
boot from one golden snapshot, serve real execs CONCURRENTLY, are
isolated from each other, and leave the golden file untouched.

    DUD_FC_METRICS=1 python dev/goldenspike.py [--clones 3]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from dud.backends.firecracker import VSOCK_PORT, FirecrackerSession
from dud.proto import Channel


def _api(sock_path: str, method: str, resource: str, body=None,
         timeout: float = 30.0):
    """One firecracker API call, over its unix socket."""
    from dud.backends.firecracker import _UnixHTTPConnection

    conn = _UnixHTTPConnection(sock_path, timeout=timeout)
    payload = json.dumps(body) if body is not None else None
    conn.request(method, resource, body=payload,
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    if resp.status >= 400:
        raise RuntimeError(f"{method} {resource} -> {resp.status}: {data!r}")
    return data


def make_golden(memory_mib: int, medium: str) -> Path:
    """Boot one VM, quiesce it, freeze it, and keep the snapshot."""
    golden = Path(tempfile.mkdtemp(prefix="dud-golden-"))
    s = FirecrackerSession(memory_mib=memory_mib, medium=medium)
    s.python("import json, os, sys")  # warm the interpreter into the image
    s.freeze()
    for name in ("vmstate", "mem"):
        shutil.copyfile(Path(s._rundir, name), golden / name)
    # Keep the boot inputs a clone needs to exist alongside it.
    shutil.copyfile(s.build.rootfs_path, golden / "rootfs")
    s.close()
    return golden


def thaw_clone(golden: Path, index: int):
    """Restore one clone from the golden snapshot, in its own rundir."""
    rundir = Path(tempfile.mkdtemp(prefix=f"dud-clone{index}-"))
    api_sock = str(rundir / "api.sock")
    uds = str(rundir / "vsock")
    listen_path = f"{uds}_{VSOCK_PORT}"

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(listen_path)
    srv.listen(128)

    log = open(rundir / "vmm.log", "wb")
    proc = subprocess.Popen(
        [shutil.which("firecracker"), "--api-sock", api_sock],
        stdout=log, stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if os.path.exists(api_sock):
            break
        time.sleep(0.01)

    if os.environ.get("DUD_FC_METRICS"):
        _api(api_sock, "PUT", "/metrics",
             {"metrics_path": str(rundir / "metrics")})

    started = time.monotonic()
    _api(api_sock, "PUT", "/snapshot/load", {
        "snapshot_path": str(golden / "vmstate"),
        # Shared, read-only in effect: firecracker maps it MAP_PRIVATE,
        # so every clone gets copy-on-write over the same golden bytes.
        "mem_backend": {"backend_type": "File",
                        "backend_path": str(golden / "mem")},
        # The whole reason this is possible at all.
        "vsock_override": {"uds_path": uds},
        "resume_vm": True,
    })
    srv.settimeout(30.0)
    conn, _ = srv.accept()
    restore_ms = (time.monotonic() - started) * 1000.0

    ch = Channel(conn)
    ch.hello_recv()
    ch.request("resync", {"epoch": time.time()})
    return {"rundir": rundir, "proc": proc, "srv": srv, "ch": ch,
            "restore_ms": restore_ms, "log": log}


def exec_python(clone, code: str) -> dict:
    body, _ = clone["ch"].request("exec_python", {
        "code": code, "inputs": {}, "timeout": 30.0, "caps": {},
        "render_budget": None, "host_objects": [],
        "outputs_hook": None, "render_hook": None,
        "cache_readonly": False, "fs_readonly": False,
    })
    return body


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clones", type=int, default=3)
    ap.add_argument("--medium", default=os.environ.get("DUD_MEDIUM", "erofs"))
    ap.add_argument("--memory-mib", type=int, default=1024)
    args = ap.parse_args()

    t0 = time.monotonic()
    golden = make_golden(args.memory_mib, args.medium)
    mem = golden / "mem"
    before = hashlib.sha256(mem.read_bytes()).hexdigest()
    print(f"golden built in {(time.monotonic()-t0):.1f}s  "
          f"({mem.stat().st_size/1e6:.0f} MB)  sha {before[:12]}")

    clones = []
    try:
        for i in range(args.clones):
            c = thaw_clone(golden, i)
            clones.append(c)
            print(f"  clone {i}: restored + serving in {c['restore_ms']:6.0f} ms")

        # All alive at once, and isolated from one another. Checked
        # through the WORKSPACE, not through globals: python state dies
        # with each runner by design, so a binding set in one exec was
        # never going to be visible in the next. (First cut of this
        # check did exactly that and reported a leak that was really
        # dud working as documented.)
        for i, c in enumerate(clones):
            r = exec_python(c, f"open('/workspace/who','w').write('{i}')")
            assert r.get("ok"), r.get("error")
        for i, c in enumerate(clones):
            r = exec_python(c, "who = open('/workspace/who').read()")
            got = r["outputs"].get("who", {}).get("v")
            print(f"  clone {i}: workspace holds {got!r} "
                  f"({'isolated' if got == str(i) else 'LEAKED'})")

        t = time.monotonic()
        exec_python(clones[0], "x = sum(range(10000))")
        print(f"  warm exec on a clone: {(time.monotonic()-t)*1000:.0f} ms")

        # The correctness question golden snapshots live or die on.
        # Every clone resumes from one frozen memory image, so anything
        # seeded before the freeze is identical in all of them —
        # firecracker's own snapshot docs warn about exactly this. If
        # os.urandom or uuid4 collide across clones, agent workloads
        # would silently share "random" values.
        probes = {
            "os.urandom": "import os; v = os.urandom(16).hex()",
            "uuid4": "import uuid; v = str(uuid.uuid4())",
            "random": "import random; v = str(random.random())",
        }
        for label, code in probes.items():
            seen = []
            for c in clones:
                r = exec_python(c, code)
                seen.append(r["outputs"].get("v", {}).get("v"))
            unique = len(set(seen))
            print(f"  {label:12s} distinct across {len(clones)} clones: "
                  f"{unique}/{len(clones)}  "
                  f"({'ok' if unique == len(clones) else 'COLLIDES'})")

        after = hashlib.sha256(mem.read_bytes()).hexdigest()
        print(f"golden mem unchanged: {before == after}")
    finally:
        for c in clones:
            try:
                c["ch"].close()
                c["proc"].kill()
                c["proc"].wait(timeout=5)
                c["log"].close()
                c["srv"].close()
            except Exception:  # noqa: BLE001 — spike teardown
                pass
            shutil.rmtree(c["rundir"], ignore_errors=True)
        shutil.rmtree(golden, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
