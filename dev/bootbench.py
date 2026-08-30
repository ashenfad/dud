"""Boot and thaw timings for the firecracker rung, as one small run.

Exists because the conformance corpus is a bad instrument for this.
It takes ~20 minutes, and what it reports is pass/fail — so iterating
on boot latency through it means waiting a third of an hour to learn
one number. This runs in a couple of minutes and prints the numbers
the decisions actually turn on.

The numbers worth watching, and why each is here:

``boot``
    Cold start, median of several. The headline.
``freeze`` / ``thaw``
    The snapshot round trip. ``thaw`` is our HTTP wall time around
    ``/snapshot/load``.
``load_snapshot``
    Firecracker's OWN measurement of the same restore, in microseconds,
    from its metrics. This is the one AWS's published baselines report
    (single-digit ms on Graviton, tens on x86), so it is the only
    figure of ours that can be compared against theirs — the gap
    between it and ``thaw`` is ours, not the VMM's.
``exec after thaw``
    A restored guest is slow for its first few execs while its working
    set is made resident again. Reported next to a warm exec, and next
    to a *shell* exec, because the split says what kind of cost it is:
    a shell touches almost nothing, CPython touches an interpreter and
    a stdlib. If only the python column inflates, the cost scales with
    the working set rather than being a fixed restore overhead.

Run it in an environment that can boot firecracker (CI, or the Lima
dev VM — see dev/fc-test.sh):

    DUD_FC_METRICS=1 python dev/bootbench.py [--medium erofs] [--boots 4]
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from pathlib import Path


def _ms(fn) -> float:
    started = time.monotonic()
    fn()
    return (time.monotonic() - started) * 1000.0


def _load_snapshot_us(rundir: str) -> float | None:
    """Firecracker's own restore latency, from its metrics stream.

    The file is newline-delimited JSON, appended on every flush, and
    the counter is cumulative — so the last non-zero reading is the
    restore we just did.
    """
    path = Path(rundir, "metrics")
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            got = json.loads(line).get("latencies_us", {}).get("load_snapshot")
        except ValueError:
            continue
        if got:
            return float(got)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--medium", default=os.environ.get("DUD_MEDIUM", "erofs"))
    ap.add_argument("--boots", type=int, default=4)
    ap.add_argument("--memory-mib", type=int, default=1024)
    args = ap.parse_args()

    from dud.backends.firecracker import FirecrackerSession

    def session():
        return FirecrackerSession(memory_mib=args.memory_mib,
                                  medium=args.medium)

    # Cold boot. The first is discarded: it may include building the
    # rootfs artifact, which is not what this measures.
    boots = []
    for i in range(args.boots):
        started = time.monotonic()
        s = session()
        boots.append((time.monotonic() - started) * 1000.0)
        s.close()
    warm = boots[1:] or boots
    print(f"medium               {args.medium}")
    print(f"boot (first, incl. any build)   {boots[0]:8.0f} ms")
    print(f"boot (median of {len(warm)})              "
          f"{statistics.median(warm):8.0f} ms   "
          f"[{min(warm):.0f}-{max(warm):.0f}]")

    s = session()
    s.python("x = 1")
    s.shell("true")
    print(f"exec warm  shell               {_ms(lambda: s.shell('true')):8.0f} ms")
    print(f"exec warm  python              {_ms(lambda: s.python('x=1')):8.0f} ms")

    for cycle in range(2):
        fr = _ms(s.freeze)
        th = _ms(s.thaw)
        sh = _ms(lambda: s.shell("true"))
        p1 = _ms(lambda: s.python("x=1"))
        p2 = _ms(lambda: s.python("x=1"))
        p3 = _ms(lambda: s.python("x=1"))
        fc = _load_snapshot_us(s._rundir)
        fc_txt = f"{fc / 1000.0:8.1f} ms" if fc else "        - (set DUD_FC_METRICS=1)"
        print(f"--- cycle {cycle}")
        print(f"  freeze                       {fr:8.0f} ms")
        print(f"  thaw (our wall time)         {th:8.0f} ms")
        print(f"  load_snapshot (firecracker) {fc_txt}")
        print(f"  exec after thaw: shell       {sh:8.0f} ms")
        print(f"  exec after thaw: python      {p1:8.0f} ms"
              f"  then {p2:.0f} / {p3:.0f} ms")
    s.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
