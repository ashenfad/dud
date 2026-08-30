"""What does ``push_tree`` cost, across tree shapes?

Exists because a default was set without it. An affinity park keeps a
whole VM alive — RAM, indefinitely — to skip exactly one thing: the
push. Everything else it might have saved is gone, because a plain miss
now restores a golden clone in 32–52 ms rather than booting. So "should
affinity parking be on by default" reduces entirely to "what is a push
worth", and that number had never been measured. It is now, and the
answer is no.

Measured on vfkit/arm64 (median of 3, one run, 2 GiB guest):

    shape                           bytes  push ms  reset+push  us/file
    tiny      10 files x 1 KB        0.0M        4          28    391.1
    small    100 files x 4 KB        0.5M        9          31     94.4
    medium   500 files x 8 KB        4.4M       35          54     70.2
    large   2000 files x 8 KB       17.4M      116         149     57.8
    chunky   100 files x 256KB      26.3M       73          96    725.4
    huge    5000 files x 4 KB       23.1M      226         277     45.3
    huge   10000 files x 4 KB       46.1M      418         512     41.8
    huge   20000 files x 2 KB       51.2M      772         890     38.6

Two findings, and then the thing they were for.

A push tracks file COUNT far more than bytes: 2000 small files cost
twice what 26 MB in 100 files does.

And it scales LINEARLY out to 20k files — per-file cost drifts *down*
(58 to 39 us across the count-scaled rows) rather than degrading — so a
push is ~40 us per file at scale, with no cliff hiding in a big tree.
The two fixed-count rows (`tiny`, `chunky`) carry per-request overhead
in their us/file and are there for the bytes axis, not the slope.

What that settles: affinity parking is OFF by default. The workspaces
this actually runs against are dozens of files, not thousands — a small
agent-generated app — which puts a push at **3-9 ms** against a ~45 ms
acquire. Keeping a 1-2 GiB guest alive to skip that is not a trade to
make on a caller's behalf. It only turns favourable around the 10k-file
mark (418 ms), which is why `max_affinity` stays a knob.

Measuring the tail was worth it even though the tail is not the case:
extrapolating from the small end predicts well over a second at 20k
against 772 ms actual, so the shape was not guessable from one end. What
settled the default, though, was knowing which end the real workload
sits at.

Run against a rung that boots (macOS/vfkit here, or the Lima dev VM
for firecracker — see dev/fc-test.sh):

    DUD_BACKEND=vfkit python dev/pushbench.py
"""

from __future__ import annotations

import argparse
import io
import os
import statistics
import tarfile
import time

#: (label, file count, bytes per file). Spread over count AND size
#: because the two are separately interesting: tar overhead and guest
#: extraction scale with entries, the socket write with bytes.
SHAPES = [
    ("tiny      10 files x 1 KB", 10, 1 << 10),
    ("small    100 files x 4 KB", 100, 4 << 10),
    ("medium   500 files x 8 KB", 500, 8 << 10),
    ("large   2000 files x 8 KB", 2000, 8 << 10),
    ("chunky   100 files x 256KB", 100, 256 << 10),
    # The tail is not optional. Stopping at 2000 made a push look like
    # a rounding error next to a clone, which is the opposite of what
    # it is for the workspaces this is actually for.
    ("huge    5000 files x 4 KB", 5000, 4 << 10),
    ("huge   10000 files x 4 KB", 10000, 4 << 10),
    ("huge   20000 files x 2 KB", 20000, 2 << 10),
]


def make_tar(n: int, size: int) -> bytes:
    """A plain tar, which is what push_tree takes and what push_dir
    builds. Incompressible payload so nothing downstream gets a free
    ride the real workload would not."""
    blob = os.urandom(size)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for i in range(n):
            info = tarfile.TarInfo(f"pkg/mod{i:04d}/file{i:04d}.bin")
            info.size = len(blob)
            tf.addfile(info, io.BytesIO(blob))
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--memory-mib", type=int, default=1024)
    args = ap.parse_args()

    backend = os.environ.get("DUD_BACKEND", "vfkit")
    if backend == "firecracker":
        from dud.backends.firecracker import FirecrackerSession as cls
    else:
        from dud.backends.vfkit import VfkitSession as cls

    s = cls(memory_mib=args.memory_mib)
    try:
        s.shell("true")  # warm the channel; don't measure the first frame
        print(f"{'shape':28s} {'bytes':>8s} {'push ms':>8s} "
              f"{'reset+push':>11s} {'us/file':>8s}")
        for label, n, size in SHAPES:
            tar = make_tar(n, size)
            pushes, cycles = [], []
            for _ in range(args.runs):
                # Reset OUTSIDE the timer: this is the push alone, the
                # thing an affinity hit actually skips.
                s._request("reset_guest", {"keep_tree": False})
                started = time.monotonic()
                s.push_tree(tar)
                pushes.append((time.monotonic() - started) * 1000)
            for _ in range(args.runs):
                # And the whole turnaround a pool miss pays, for scale.
                started = time.monotonic()
                s._request("reset_guest", {"keep_tree": False})
                s.push_tree(tar)
                cycles.append((time.monotonic() - started) * 1000)
            push = statistics.median(pushes)
            print(f"{label:28s} {len(tar) / 1e6:7.1f}M {push:8.0f} "
                  f"{statistics.median(cycles):11.0f} {push * 1000 / n:8.1f}")
    finally:
        s.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
