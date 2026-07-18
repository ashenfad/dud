# Roadmap

Companion to [DESIGN.md](DESIGN.md) (rationale) and [PLAN.md](PLAN.md)
(the original staging). PLAN froze the ladder before any code existed;
this doc is the live view — what's built, what's next, and which items
unblock which. Updated 2026-07-18.

## Where we are

The macOS VM rung is real. The ladder today:

| rung | status |
|---|---|
| `subprocess` | shipped — dev/CI floor, zero isolation |
| `vfkit` (macOS/HVF) | **shipped** — 48/48 conformance green over vsock |
| `firecracker` (Linux/KVM) | designed, not started (no Linux host here) |

What that means concretely, end to end:

- **Images**: `dud.images` pulls an OCI base (dependency-free registry
  client), layers pip packages as prebuilt guest-arch wheels
  (`uv pip install --target`, cross-platform), injects the dud guest
  runtime, and emits a root-owned newc initramfs — all in pure Python,
  cached by spec hash under `~/.dud`. The DS stack
  (numpy/pandas/pyarrow/matplotlib/plotly) runs inside the VM.
- **Boot**: ~3 s warm to a served vsock channel (~0.45 s kernel→init;
  the rest is the guest's dial retry). `exec_python` ≈ 30 ms per call.
  First-ever DS image build ≈ 40 s, then cached.
- **Integration**: nontainer's `DudExecutor` selects the rung
  (`backend="subprocess" | "vfkit"`); studio toggles via
  `NONTAINER_STUDIO_EXECUTOR=dud | dud-vm`. Checkpoint/restore/fork,
  apps GET/POST dispatch (contract crosses by source), and rich-`ui`
  artifact generation all work over the VM.

Branch state (ashenfad drives merges): dud `stage-0` holds everything
here; nontainer `executor-seam`+`dud-executor` are merged to nxt,
`apps-vm-contract` is pending; studio `dud-executor` is pending.

## Short term

Ordered by unblock-value, not strict sequence. The kernel spike is the
lynchpin — three deferred items all trace back to it.

### 1. Guest kernel with `CONFIG_OVERLAY_FS` (+ virtiofs, virtio-rng)

The current kernel is puipui — a minimal VZ-test kernel that proved
boot but can't grow with us: no overlayfs (probed live: both 5.15.71
and 6.11.5 lack it), no virtio-rng driver (hence the `PYTHONHASHSEED=0`
cmdline workaround), no virtiofs. One kernel spike (source or build an
arm64 `Image` with those three) unblocks:

- **Overlay `/workspace` at the root** (the deferred stage 4-4):
  lower = pushed snapshot ro, upper = the diff, harvested directly —
  O(changes) diffs instead of O(tree) scan, a *true* read-only mount
  for GET views (today: post-hoc diff check + reset), and the
  workspace mounted at `/` — which also fixes the absolute-path
  fidelity gap (a handler writing `/app/x` today lands in the VM's
  throwaway root, silently outside the diff).
- **virtiofs lowerdir**: large workspaces mounted from the host
  instead of tarred over vsock every session.
- **Real entropy**: retire the hash-seed workaround.

Also owed here: a kernel *distribution* story — today the kernel is a
hand-placed file at `~/.dud/kernels/<arch>/Image`; it should be a
versioned, fetched-and-cached dud asset per arch.

### 2. ext4 rootfs medium (demand-paged images)

The DS initramfs costs ~400 MB of guest RAM per VM because the whole
rootfs is RAM-resident. The medium seam is already in place
(`meta.json` + spec hash + `_medium_boot_args`), so ext4-on-virtio-blk
is additive: host page cache shared across VMs of the same image, RAM
proportional to pages touched. Build strategy is the open question —
no `mke2fs` on macOS; the self-hosting option (boot a dud VM, build
the ext4 image *inside it*) is now viable and keeps the
zero-host-dependency property. Size-based auto-selection (small →
initramfs, big → ext4) once both exist.

### 3. VM lifecycle hardening

Cheap, high-value, mostly host-side:

- **Idle eviction**: studio holds a warm VM per open session forever
  (2–4 GB each). Evict after idle; state is all in kvgit, so revival
  is a ~3 s boot + tree push.
- **Death recovery**: a dead VM currently means dead-session errors
  until reopen. The disposable thesis makes auto-reboot trivial —
  detect channel loss, boot, re-push, retry the call once.
- **Boot latency**: ~2.5 s of the 3 s is the guest retrying its vsock
  dial until vfkit's bridge is ready. Find or add a readiness signal.

### 4. Dogfood gate, now with real walls

The behavioral half of the stage-3 gate (`dogfood/analyst_agent.py`,
needs a model key) was never run — we proceeded on the empirical
fidelity evidence. Running it under `dud-vm` is now *more* informative
than the original plan: real fidelity **and** real isolation, versus
sandtrap, on the studio's actual analyst loop.

### 5. Ship track

- Merge the stack (dud `stage-0` → main; nontainer
  `apps-vm-contract`; studio `dud-executor`).
- PyPI: publish dud, flip nontainer's and studio's sibling-path
  sources to real deps, decide the version-pinning policy between the
  three.

## Medium term

### Firecracker rung (PLAN stage 5)

Needs a Linux+KVM host — untestable on this Mac, so it starts as a
write-and-ship-to-Linux exercise: jailer, no-NIC default, cgroup
budgets, and the piece vfkit can't offer — **snapshot/restore**
(resume ≈ 5–20 ms, CoW memory), which changes the warm-pool economics
entirely. The conformance suite is the contract; the backend should be
mostly `HostSession` + transport, like vfkit was.

### Serving (PLAN stage 6)

Today an "app" is inseparable from its authoring session — every
GET/POST is an exec in the session's own VM, serialized with the
agent's calls. Serving splits that:

- **Read-only dispatch pool**: N VMs booted from a *published* kvgit
  snapshot, GET-only dispatch, no writer, disposable or warm-pooled.
- **In-guest serving / real `curl`**: the remaining agent-loop gap
  (the terminal `curl` is a termish builtin; real bash has neither the
  binary nor a server to hit). Either an in-guest router + real HTTP
  port, or a wire-level shell→host bridge. Agents use
  `test_app`/preview meanwhile.
- **hostcall codec hardening**: the boundary's attack surface, before
  anything serves untrusted traffic.

### CI matrix

Conformance runs per-rung locally (`DUD_BACKEND=vfkit`). CI needs:
subprocess everywhere (works today); vfkit on macOS arm64 — open
question whether GitHub's hosted runners allow Virtualization.framework
(nested virt); may need a self-hosted Mac. Firecracker needs a
KVM-capable Linux runner. Golden transcripts (PLAN's cross-cutting
item) become valuable exactly here — pinning agent-visible runner
semantics across rungs in one corpus.

## Deliberately not now

- **Rung 1.5 (Seatbelt/Landlock around the subprocess rung)** — the VM
  rung landing on macOS removed most of its audience; revisit only if
  a Linux-dev-without-KVM constituency appears.
- **Per-blob content addressing** — kvgit's concern (storage v4
  candidate), not dud's.
- **Windows** — no rung, no plan.
- **Incremental tree push** — `push_tree` wipes and re-extracts on
  restore; fine at current workspace sizes, superseded by virtiofs
  for large ones.
