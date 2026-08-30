"""What does ``push_tree`` cost, across tree shapes?

Exists because one default was set without it. An affinity park keeps a
whole VM alive — RAM, indefinitely — to skip exactly one thing: the
push. Everything else it might have saved is gone, because a plain miss
now restores a golden clone in 32–52 ms rather than booting. So the
question "is ``max_affinity=1`` a sensible default" reduces to "what is
a push worth", and that number had never been measured.

Measured on vfkit/arm64 (median of runs), against a 1-2 GiB guest:

    shape                            bytes   push (ms)   us/file
    tiny      10 files x 1 KB         0.0M          3       300
    small    100 files x 4 KB         0.5M          9        90
    medium   500 files x 8 KB         4.4M         34        68
    large   2000 files x 8 KB        17.4M        104        52
    chunky   100 files x 256KB       26.3M         50       500
    huge    5000 files x 4 KB        23.1M        196        39
    huge   10000 files x 4 KB        46.1M        377        38
    huge   20000 files x 2 KB        51.2M        690        35

Two findings, and the second is the one that matters.

A push tracks file COUNT far more than bytes: 2000 small files cost
twice what 26 MB in 100 files does.

And it scales LINEARLY out to 20k files — per-file cost drifts *down*
(51 to 35 us) rather than degrading, so there is no cliff and no
surprise waiting in a big workspace. That is what makes the number
usable as a default-setting input: a push is ~40 us per file, all the
way up. A 500-file scratch tree costs 26 ms; a 10k-file workspace with
its dependencies installed costs 377 ms.

The first cut of this bench stopped at 2000 files and reported "40-140
ms, modest", which nearly argued the default to 0. Extrapolation would
have been wrong in the other direction too (linear from the small end
predicts ~1 s at 20k, against 690 ms actual). Measuring the tail was
the whole point.

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
        print(f"{'shape':28s} {'bytes':>9s} {'push (ms)':>11s} "
              f"{'reset+push':>11s}")
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
            print(f"{label:28s} {len(tar) / 1e6:8.1f}M "
                  f"{statistics.median(pushes):10.0f} "
                  f"{statistics.median(cycles):10.0f}")
    finally:
        s.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
