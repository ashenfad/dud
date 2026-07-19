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

### 1. ~~Guest kernel with `CONFIG_OVERLAY_FS` (+ virtiofs, virtio-rng)~~ shipped

The kernel spike landed without a build: the **Kata Containers release
kernel** (6.18, arm64 `Image`, the same kernel Apple's containerization
stack recommends for Virtualization.framework guests) has everything
`=y` — overlayfs (mount-verified in-guest), virtiofs, virtio-rng
(`PYTHONHASHSEED=0` workaround retired), vsock/blk/console, ext4.
`dud.kernels` makes it a versioned, digest-pinned, fetched-and-cached
asset (`python -m dud.kernels`; needs `zstd` at fetch time only), and
the registry client now caches manifest resolution so cached images
survive Docker Hub rate limits and work offline.

Unblocked and now actionable:

- ~~**Overlay workspace staging**~~ **shipped** (stage 4-4's diff
  half): on VM rungs `work/` is an overlayfs mount — lower = pushed
  snapshot, upper = the diff, harvested directly with scan-diff parity
  (per-file delete expansion, identical-copy-up suppression, one
  conformance corpus over both producers; `ping` reports the live
  staging so tests refuse silent fallback). Measured on a 210 MB tree:
  diff-with-one-change 0.13 s → **0.001 s**, `reset_stage` → **~0 s**,
  and baseline RAM cost drops from 2x tree to 1x + changes.
  `fs_readonly` exec windows are a *true* r/o remount on this rung
  (rung 1 keeps the documented post-hoc gap).
- **Workspace at `/`** — the deferred other half of 4-4: mounting the
  workspace at the root to close the absolute-path fidelity gap (a
  handler writing `/app/x` lands outside the diff today). Deferred
  because it changes guest path/cwd semantics that nontainer and
  studio observe (`__cwd__` persistence, sandtrap parity) — needs the
  surface layers at the table, not a dud-only decision.
- **virtiofs lowerdir**: large workspaces mounted from the host
  instead of tarred over vsock every session.

The kernel is rehosted as a dud release asset
([kernel-kata-3.32.0](https://github.com/ashenfad/dud/releases/tag/kernel-kata-3.32.0),
provenance + GPL source pointers in the notes): an 18 MB direct
download instead of Kata's 664 MB tarball, no `zstd` needed for the
pinned path. While the repo is private, anonymous downloads 404 and
the fetcher falls back to the authenticated `gh` CLI — making the repo
public (already implied by the PyPI ship-track item) lets anonymous
fetch just work.

### 2. ~~erofs rootfs medium + self-hosted builder~~ shipped

Wired end to end: `medium="erofs"` (or `"auto"`: packages present or
base > 100 MB compressed → erofs) builds the fileset, bakes it inside
a builder VM (slim + erofs-utils via pinned debs), and boots it as a
demand-paged read-only root (`root=/dev/vda`; an empty initrd appeases
vfkit's CLI flag-group; the kernel falls through to the block root).
Each VM attaches a per-boot APFS CoW clone — VZ exclusively locks r/w
attachments and vfkit's virtio-blk exposes no readOnly flag despite
the VZ API having one (upstream PR opportunity; would also restore
cross-VM page-cache sharing). Measured on the DS image, warm caches:
boot 1.8 s → **0.9 s**, guest RAM at boot 600 MB → **79 MB**, after
importing the full DS stack 636 MB → **167 MB**. Conformance: 56/56
on both media. Studio still defaults to initramfs — flipping to
`auto` is a one-arg decision bundled with the eager-warming work.
Known ceiling: the self-hosted build transiently holds the fileset,
its tar, the extracted guest tree, and the returned image (~several
GB total for very large images) — fine at DS scale; streaming the
push from disk and the virtiofs lowerdir both shrink it when needed.

### (original notes)

The DS initramfs costs ~400 MB of guest RAM per VM because the whole
rootfs is RAM-resident. The medium seam is already in place
(`meta.json` + spec hash + `_medium_boot_args`), so a block-device
medium is additive: host page cache shared across VMs of the same
image, RAM proportional to pages touched.

The format is **erofs, not ext4** (decided 2026-07-18): the root is
immutable by thesis, and erofs is read-only *structurally* — no write
path to misuse — smaller (compressed images stay page-cache-shared),
built into the pinned kernel, and where the prior art converged
(Kata/nydus/composefs). ext4's one differentiator, writability, is a
non-goal; it re-enters only if a disk-backed *writable* layer is ever
needed — additive through the same seam.

Build strategy: **self-hosted** — no `mkfs.erofs` on macOS, so boot a
dud VM whose image carries `erofs-utils` (guests have no network; the
tool arrives via pinned-.deb layering, the wheels trick for system
packages), push content in, build inside, pull the image back through
the diff. One investment, three payouts: erofs rootfs images, frozen
published-app workspace artifacts (the stage-6 read-only dispatch
substrate — a VM fleet demand-paging one content-addressed file per
app, immutability physical rather than procedural), and any future
format without host tooling. Size-based auto-selection (small →
initramfs, big → erofs) once both exist.

### 2b. View-exec latency (preview GETs)

Baked hash-based ``.pyc`` into layered packages (pipeline v2) — every
exec was recompiling its imports from source (~1 s per preview GET,
almost all pandas). Measured after: session switch on a warm pool
0.65 s; preview API GETs 370–580 ms (was ~1 s). The remaining floor
is per-exec interpreter spawn + pandas module init (~0.4 s). Next
lever if it matters: a session-persistent read-only **view worker**
in the guest — imports warm, fresh namespace per request (sandtrap
served views from a warm process too, so this is parity, not a
cheat). Pairs naturally with the stage-6 dispatch pool.

### 3. VM lifecycle hardening

Cheap, high-value, mostly host-side:

- ~~**Cross-session reuse**~~ **shipped**: `VmPool` keys warm VMs by
  boot fingerprint; same-spec sessions reopen in ~0 s (guest reset +
  tree push) instead of booting. Hygiene on release (`reset_guest`:
  trees wiped, boot env restored, stray guest processes killed);
  out-of-workspace residue survives by design within one user's
  studio. In-process only — a parked VM still dies with the server.
- **Restart survival**: the companion piece pooling exposes — detached
  VMs + guest reconnect-on-EOF (today the guest powers off when the
  channel drops) + deterministic socket paths, so warm VMs outlive
  studio restarts. Needs a small vfkit re-bridge spike.
- **Idle eviction**: studio holds a warm VM per open session forever
  (2–4 GB each). Evict after idle; with the pool, eviction is just
  early release — revival is a tree push.
- **Death recovery**: a dead VM currently means dead-session errors
  until reopen. The disposable thesis makes auto-reboot trivial —
  detect channel loss, boot, re-push, retry the call once.
- **Boot latency**: ~2.5 s of the 3 s is the guest retrying its vsock
  dial until vfkit's bridge is ready. Find or add a readiness signal.
  Matters less per-session now that reuse skips boots, but still paid
  on pool misses and first opens.

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
