# Roadmap

Companion to [DESIGN.md](DESIGN.md) (rationale) and [PLAN.md](PLAN.md)
(the original staging). PLAN froze the ladder before any code existed;
this doc is the live view — what's built, what's next, and which items
unblock which. Updated 2026-07-19.

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
- ~~**Workspace at the root**~~ **shipped** (2026-07-20) — the
  deferred other half of 4-4, decided WITH the surface layers at the
  table (nontainer/studio settled on `/workspace` as the one
  agent-visible root across executors; a guest can't mount at `/`, a
  VFS can present any prefix, and `/workspace` is the strongest
  training prior). The merged overlay now mounts AT the configured
  root (`dud.root`, default `/workspace`) and the staging trees moved
  to a stash tmpfs at `/run/dud-stage` — so the agent-visible root
  contains exactly the workspace, absolute writes like
  `/workspace/x` land in the upper (in the diff — the fidelity gap,
  closed), and the backing trees are out of guest reach (mutating a
  mounted overlay's lower/upper is kernel-UB; they used to sit
  exposed inside the root). Rung 1 keeps `root/work` (a host temp
  dir can't claim `/workspace`; documented dev-posture gap).
- **virtiofs lowerdir**: large workspaces mounted from the host
  instead of tarred over vsock every session. Deferred indefinitely
  (2026-07-19): the scratch plane (3b) routes bulk to disk-cache, so
  state stays small — kvgit's own assumption — and eager hydration
  (~0.4 s / 200 MB) stops mattering. Revisit only if large *source*
  data (user uploads that are genuinely state) becomes common.

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

### 2b. ~~View-exec latency (preview GETs)~~ shipped

Two levers, both landed:

- **pyc bake (pipeline v2)**: hash-based ``.pyc`` baked into layered
  packages — every exec was recompiling its imports from source
  (~1 s per preview GET, almost all pandas). GETs → 370–580 ms.
- **View worker** (2026-07-19): view execs (``fs_readonly``) fork
  from a warm import **template** (``dud.guest.template``, VM rung
  only) instead of paying interpreter spawn + module init per
  request. The template imports the image's installed packages once
  at boot (background; spawn-path until ready — ``ping`` reports
  ``view_worker``), never runs user code, and forks per request:
  CoW-warm imports, genuinely fresh namespace, child exits after one
  request. Timeout kills the child's group; ``reset_guest`` kills and
  re-warms the template; a dead template degrades to the spawn path
  and re-warms. Observable via ``DUD_VIEW_WORKER=1`` in the exec env
  (conformance pins routing, freshness, read-only enforcement,
  timeout, and death-recovery). Measured (pandas image, erofs):
  exec 250 ms (spawn) → **117 ms** (worker, *including* the real
  read-only remount); the gap widens with import weight — the worker
  is flat where spawn scales with the stack. Zero public API change.
  The remaining floor is fork + remount + channel; the stage-6
  dispatch pool inherits this machinery wholesale.

### 3. ~~VM lifecycle hardening~~ shipped (2026-07-19)

The organizing insight: **death recovery is the primitive, not a
resilience feature**. Once any VM can vanish at any moment and its
owner recovers uniformly (re-acquire + re-push + retry once), idle
eviction and capacity pressure stop being separate mechanisms — they
are just deliberate deaths.

- ~~**Cross-session reuse**~~ **shipped**: `VmPool` keys warm VMs by
  boot fingerprint; same-spec sessions reopen in ~0 s (guest reset +
  tree push) instead of booting. Hygiene on release (`reset_guest`:
  trees wiped, boot env restored, stray guest processes killed);
  out-of-workspace residue survives by design within one user's
  studio.
- ~~**Death recovery**~~ **shipped**: transport failures surface as
  one typed error (`SessionLost`, raised at `HostSession._request`,
  the single wire seam) and `DudExecutor` recovers in place — reopen
  (warm-pool acquire), re-push the provider tree, re-assert cwd,
  retry the call once. At-most-once-observed for state: the dead
  attempt's diffs never landed and cache writes only commit on
  success (repeated live-object hostcalls are the documented residue).
- ~~**Capacity / idle eviction**~~ **shipped as demand-driven
  reclaim**: sessions keep their VM bound across calls (zero per-call
  tax; background processes and warm imports survive), but the pool
  tracks bound VMs and — under a `max_total` cap (`$DUD_VM_MAX_TOTAL`,
  opt-in) — reclaims before booting past it: global-LRU *idle* victim
  first, then the LRU *bound* VM not mid-request. The reclaimed owner
  pays ~1 s on its next call via the recovery path. No timers guessing
  at idleness; cost lands only under real pressure, on whoever was
  quiet longest. Idle buckets serve MRU (hottest page cache first) so
  excess warmth ages out via TTL.
- ~~**Crash hygiene**~~ **shipped**: rundirs record their vfkit pid;
  the first boot in a process sweeps orphaned rundirs (sockets, logs,
  APFS rootfs clones) whose recorded vfkit is gone — the on-disk
  counterpart of the process-linkage invariant below.
- **Boot latency**: ~2.5 s of the 3 s initramfs boot is the guest
  retrying its vsock dial until vfkit's bridge is ready (erofs boots
  in ~1 s). Find or add a readiness signal. Matters less now that
  reuse skips boots and recovery is rare, but still paid on pool
  misses and first opens.

### 3b. ~~The scratch plane~~ shipped (2026-07-19)

Disk as a cache for computation (DESIGN.md "The scratch plane"):
``VfkitSession(scratch=master.img)`` attaches a per-boot CoW clone of
an ext4 volume mounted over ``/tmp`` — cache, not state (never in
diffs/commits/forks; promoted to the master only on clean
park/shutdown, discarded on crash; ext4 journal = no guest fsck).
``dud.images.scratch.blank_ext4(size)`` bakes the blank master
(self-hosted mke2fs via pinned e2fsprogs debs; native tool when the
host has one), cached per size class. Scratch is part of the boot
fingerprint, so pooled VMs never leak cache across keys. Intended
customer: published apps (frozen commit → cache can't go stale) —
wiring the key choice (token/commit → master path) into
nontainer/studio is the remaining surface work. This deliberately
replaced the lazy-hydration track: state stays small and eager
(kvgit's own assumption), computation residue gets the disk.

### 4. Dogfood gate, now with real walls

The behavioral half of the stage-3 gate (`dogfood/analyst_agent.py`,
needs a model key) was never run — we proceeded on the empirical
fidelity evidence. Running it under `dud-vm` is now *more* informative
than the original plan: real fidelity **and** real isolation, versus
sandtrap, on the studio's actual analyst loop.

### 5. Ship track

- ~~API façade~~ **done** (2026-07-19): one front door —
  ``dud.session(backend=..., pooled=..., state=...)`` absorbs the
  backend switch every consumer was writing themselves (``"vm"``
  = best VM rung for this host, so configs survive the firecracker
  rung landing); lazy top-level re-exports (``VfkitSession``,
  ``SessionLost``, ``IsolationUnavailable``, ``scratch_master``, ...);
  a ``DudError`` spine under every public exception (historical bases
  kept — existing ``except`` clauses unaffected);
  ``scratch_master(key)`` settles scratch keying (per-key masters
  under ``~/.dud/scratch/keys/``, the future GC seam);
  ``close(park_state=...)``; result ``__bool__``; ``medium`` defaults
  to ``"auto"``. Deep import paths keep working.
- Merge the remaining stacks (nontainer `vm-recovery`; studio
  `dud-executor`) — deliberately after the dud API is firm and on
  PyPI.
- PyPI: publish dud, flip nontainer's and studio's sibling-path
  sources to real deps, decide the version-pinning policy between the
  three.

## Medium term

### Firecracker rung (PLAN stage 5) — boots, in progress

The "untestable on this Mac" premise died: Apple silicon M3+ nested
virtualization gives a Lima guest a real /dev/kvm, so the rung is
developed and conformance-tested locally (`dev/lima-fc.yaml`;
firecracker binary + native mkfs.erofs/mke2fs inside). The backend
landed as designed — `HostSession` + transport: firecracker's
HTTP-over-UDS API plane, guest-dial vsock via the `<uds>_<port>`
convention, same pinned kernel asset. Three deltas from vfkit, all
simplifications: erofs roots attach **read-only** (no per-boot clone;
cross-VM page cache restored), no empty-initrd appeasement, extra
disks attach truly read-only. `session(backend="vm")` resolves per
platform, so consumer config never changes.

Still open on this rung:

- **Pooling / snapshot-restore — SHIPPED (2026-07-19, freeze/thaw)**:
  `freeze()` parks a running VM as files (guest-cooperative `freeze`
  verb → pause → `/snapshot/create` → kill VMM; a `frozen` marker
  carrying the host pid keeps the sweep off the bundle) and `thaw()`
  resumes in a fresh VMM (`/snapshot/load`, mmap'd memory file, guest
  redials vsock, `resync` fixes the wall clock and re-warms the fork
  template for PRNG uniqueness across clones). The pool duck-types the
  posture: vfkit parks hot (reset + keep running), firecracker parks
  frozen (reset + freeze; thaw on acquire) — same acquire/release
  contract, same affinity tags, and frozen parks are invisible to
  `max_total` (files, not RAM). A bare channel EOF still means "die";
  only an acked freeze enters the guest's bounded redial loop, so the
  no-dangling-VMs invariant survives. Still open here: golden
  snapshots per fingerprint (boot-once → freeze-clean → every pool
  miss thaws a clone instead of cold-booting).
- **Hardening posture**: jailer, no-NIC default, cgroup budgets — the
  production-grade wrapper, not needed for the dev rung.
- **amd64 pins** (kernel `vmlinux`, debs) — wanted for CI anyway:
  GitHub's `ubuntu-latest` runners have /dev/kvm, so firecracker
  conformance can run in plain hosted Actions on x86-64.

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

- **Restart survival (detached VMs)** — decided against (2026-07-19).
  VM lifetime is process-linked *as an invariant*: channel EOF powers
  the guest off, vfkit exits with the guest, so even a `kill -9` of
  the host cascades to full cleanup with no orphan reaper. Detaching
  would remove exactly that safety and rebuild its guarantees by hand
  (socket rediscovery, adopt protocols, stale-guest-runtime
  handshakes) to save a ~1 s boot whose tree would be re-pushed from
  kvgit anyway. The future where durable warmth matters is the
  firecracker rung, where it arrives structurally: snapshots are
  files, and files survive restarts without any process outliving
  anything.
- **Rung 1.5 (Seatbelt/Landlock around the subprocess rung)** — the VM
  rung landing on macOS removed most of its audience; revisit only if
  a Linux-dev-without-KVM constituency appears.
- **Per-blob content addressing** — kvgit's concern (storage v4
  candidate), not dud's.
- **Windows** — no rung, no plan.
- **Incremental tree push** — `push_tree` wipes and re-extracts on
  restore; fine at current workspace sizes, superseded by virtiofs
  for large ones.
