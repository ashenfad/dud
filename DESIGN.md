# dud 🧨

*A dumb firecracker.* Real, disposable microVMs as an execution substrate
for [nontainer](https://github.com/ashenfad/nontainer)-style versioned
workspaces — `pip install dud`, no Kubernetes, no daemon, no cloud.

> **Status: design.** Nothing here is implemented. This document is the
> distillation of the design discussion that led to the repo.

## Thesis

nontainer's stack virtualizes **state, not the machine**: the workspace is
versioned, forkable, content-durable data (kvgit), and the "computer" the
agent uses is an emulation good enough to work against that state —
termish for the shell, monkeyfs for the filesystem, sandtrap for gated
Python.

dud is the observation that the state layer never depended on the machine
being fake. Because nontainer uses a **script model** (no resident REPL —
state lives in files + cache, never in a live heap), the machine is
stateless between calls. So a *real* machine — a microVM booted from a
real Linux image — can slot in as the executor, and:

- the emulation layers gracefully retire: real bash instead of termish,
  a real filesystem instead of monkeyfs, a VM boundary instead of
  sandtrap's AST gates;
- the versioning semantics (checkpoint per call, O(1) fork, rollback,
  merge, audit) survive **unchanged**, because they were always
  properties of the state layer, not the machine;
- the fidelity walls fall: C extensions, sqlite on real files, `pip
  install`, esbuild/node, memory-mapped parquet — the exact workloads
  the emulation stack serves worst are the data-science workloads
  nontainer-studio ships as its flagship loop.

The emulation stack was scaffolding for the workspace contract on
machines we couldn't afford to make real. A real machine retires the
scaffolding, not the contract.

## Dumbness as a design principle

dud is deliberately dumb. It knows nothing about kvgit, checkpoints,
forks, tool descriptions, or app dispatch. Its entire contract is:

```
tree in  →  execute stuff against a real filesystem  →  diff out
```

**Versioning stays in nontainer's `WorkspaceProvider`** (kvgit, AgentFS,
a plain directory — or, amusingly, a plain git repo, since tarball-in /
diff-out is git's native vocabulary). nontainer's `Workspace` does the
linking: provider materializes the tree at HEAD → dud executes calls →
dud yields the diff → provider stages and commits it.

This is the line every adjacent system couples across (see Prior art):
they version the *machine* — opaque memory+disk snapshots — where this
design versions the *state* and treats machines as fungible. Keeping dud
storage-blind is what preserves that position. If dud ever grows a
storage dependency, the design has failed.

The composite (nontainer + dud) behaves like a "versioned VM." dud alone
is just a machine that boots, diffs, and dies.

## The two seams in nontainer

1. **`WorkspaceProvider`** (exists) — fs + kv + versioning verbs, gated
   by capability flags. Unchanged by dud.
2. **`Executor`** (to be carved) — execution is currently welded to
   sandtrap inside `Workspace` (`build_sandbox`, `PythonConfig` as
   sandtrap-Policy sugar). dud forces the seam out, the same way AgentFS
   forced `WorkspaceProvider`. sandtrap becomes the default
   `LocalExecutor`; dud is the second implementation. The agent-facing
   tool surface (`terminal`, `run_python`, cache, checkpoints on
   results) is identical across executors.

Rough shape:

```
Executor:
    open(tree: TreeSource) / close()
    exec_shell(script, env, cwd)   -> ShellResult
    exec_python(code, inputs)      -> ExecResult      # runner-based; see Outputs
    diff()                         -> StagedDiff      # harvested at checkpoint
    reset()                                           # discard staged writes
```

## Execution model

The workspace mounts in the guest at `/workspace` as **overlayfs**:
lowerdir = the materialized snapshot (read-only), upperdir = the staging
area. The upperdir *is* kvgit's staged buffer, implemented by the
kernel:

- `diff()` harvests the upperdir (with whiteouts for deletes) — an exact
  staged diff, no mtime scanning;
- `reset()` wipes the upperdir — `discard()` semantics for free;
- nontainer's atomicity rule ("a handler that writes three files then
  raises leaves nothing behind") survives as mount-level operations.

Session lifecycle exploits the script model: the VM is a **pure
performance artifact**. Warm while the session is active; killed when
idle (nothing is lost — state is in the provider); resumed from a base
snapshot on the next turn. Fork = provider branch (O(1)) + fresh VM
pointed at it. Restore = re-materialize a different commit. VM lifetime
is a tuning knob, never a correctness concern.

## The scratch plane: disk as a cache for computation

A guest sees three planes, with progressively weaker guarantees and
progressively cheaper writes:

| plane | where | guarantee |
|---|---|---|
| **state** | the workspace | committed, versioned, the *complete* description of a session |
| **cache** | the kv `cache` | managed, transactional (writes land only on successful execs) |
| **scratch** | `/tmp` | best-effort memoization; may vanish at any moment |

The scratch plane is the formalization of what was previously
"out-of-workspace residue": a writable ext4 volume mounted over
`/tmp`, whose contents are **cache in the strict sense** — derived,
re-derivable, and droppable without correctness consequences. The
commit remains the complete guarantee of state *because* scratch is
structurally excluded from it: scratch never appears in diffs, never
enters commits, is never restored, and a fork always starts cold.
Anything an app must not lose does not belong in `/tmp`.

The intended customer is a **published app** (a frozen snapshot): its
commit never advances, so a scratch keyed to it can never go stale —
materialize an index on the first query, serve every later query
fast. Interactive sessions get VM-lifetime scratch only (their commit
moves every checkpoint; don't version scratch against history — that
way lies blurring the state guarantee).

Mechanics (VM rungs): a blank sparse ext4 **master** is baked once
per size class (self-hosted `mke2fs`, like the erofs build; native on
hosts that have it). Each boot attaches a CoW **clone** of the
caller's master and mounts it over `/tmp` — nothing is copied, blocks
materialize as touched. On a *clean* park or shutdown the clone is
promoted back to the master (clonefile + atomic rename;
last-clean-wins — it's cache). A crashed VM's clone is discarded with
its rundir, and the ext4 journal means a promoted-mid-flight volume
mounts clean in-kernel, no guest fsck. The master's path is part of
the VM's **boot fingerprint**, so a pooled VM only ever serves
sessions keyed to the same scratch — no cross-key cache leakage
through reuse.

Two properties keep the contract honest. First, the disposable thesis
*polices* it: VM death, pool reclaim, and eviction clear scratch
constantly and randomly, so code that secretly depends on it breaks
in development, not production. Second, scratch is a shared surface
within its key — handlers serving multiple viewers must not stash
per-viewer secrets in `/tmp`, the same rule as any server-side cache.

## Wire protocol

One channel between host and guest supervisor (vsock for VMs, unix
socket for the subprocess backend), msgpack-framed, with services
multiplexed on it:

| verb | direction | purpose |
|---|---|---|
| `push_tree` | host → guest | materialize `/workspace` (tar) |
| `exec_shell` | host → guest | run script; returns transcript, exit, new cwd/env |
| `exec_python` | host → guest | run code via the Python runner |
| `pull_diff` | host → guest | harvest upperdir + whiteouts |
| `reset_stage` | host → guest | wipe upperdir |
| `emit` | guest → host | fire-and-forget structured output (see Outputs) |
| `cache.get/put` | guest → host | the KV plane as a service |
| `hostcall` | guest → host | invoke an allowlisted host-object method |
| `ping` / `shutdown` | host → guest | lifecycle |

**No pickle ever crosses this boundary.** Cache values are pickled and
unpickled only guest-side; the host stores opaque bytes. The only
host-side deserialization surfaces are `hostcall` arguments and `emit`
payloads, both restricted to a typed codec (msgpack over an allowlisted
type set). This closes, by construction, the worker→host pickle hole
documented in sandtrap's threat model — the boundary is only as strong
as this rule.

## Outputs: emits, not namespaces

`result.namespace` — sandtrap's harvest of top-level bindings — is the
one Python-specific concept in the current contract, and it assumes live
objects can reach the host. Both assumptions die here. The script model
makes this painless: namespace was never *state* (state is cache +
helpers + files), only a per-call return channel. So name the channel:

```
ExecResult { transcript, exit_code, outputs: {name → Value} }

Value = json(...)                  # the floor
      | bytes(mime, data)          # small binary
      | file(path)                 # ref into /workspace — lands in the commit
      | chart(spec) | table(spec)  # declarative UI (plotly JSON, columnar)
```

Population is per-runtime sugar over the language-neutral `emit`
service (a well-known fd/socket in the exec environment — **not stdout
markers**, which agent code echoing untrusted data could forge):

- **Python runner**: harvests top-level bindings post-exec, emits what's
  codec-representable. The `ui = {...}` convention works unchanged;
  figures flatten guest-side (`fig.to_json()`) — relocating logic the
  studio frontend already has, not inventing it.
- **Node runner** (future): harvest `exports` or explicit `emit()`. This
  turns nontainer's deferred run-ts sidecar into an image + a small
  runner — no core changes.
- **Bash / anything**: a `dud-emit` CLI in the image, or write files and
  return refs.

`inputs=` gets the symmetric treatment: tagged values in, reconstituted
as bindings by the Python runner, read explicitly elsewhere.

**Forcing function**: spec the emit channel so `terminal`'s bash can use
it. Bash has no namespace, no objects, no pickle — if the contract is
ergonomic from bash, it's language-neutral by construction.

**Accepted loss**: arbitrary live Python values out of `run_python`.
Embedders who need rich values back use `file` refs and deserialize
host-side as an explicit, trust-aware act — never an ambient one.

### The Python runner (run_python without sandtrap)

sandtrap did two jobs; only one needed its machinery. **Policy** (AST
rewriting, gates, builtins whitelist) is replaced by the VM. **Plumbing**
(namespace injection, cache, harvest, echo) is ordinary Python — the
runner is ~150 lines:

- Build a globals dict: decoded `inputs` as bindings; `cache` as a
  read-through/write-back view over the channel; each host-object
  registration as a **dumb proxy** whose `__getattr__` returns a
  hostcall-marshalling callable. Proxies are safe *by construction*:
  there is no real object in guest memory to traverse to — agent code
  can only produce hostcalls, which the host validates against the
  registration allowlist. (v0: methods return codec values only, no
  live sub-handles.)
- Plain `exec` — no rewriting. Last-expression echo via a ten-line
  `ast` split (the runner's only ast use; same trick as Jupyter's
  `last_expr`). `print` shadowed for raw-object capture into the
  prints stream.
- Harvest top-level bindings post-exec (minus injected names and
  underscores) into the `Value` codec.
- Cache writes buffer guest-side and flush **on successful completion
  only** — atomic with the call's checkpoint, matching staging
  semantics (a raise leaves nothing behind, cache included).
- `helpers/` needs no VFS import hook: `/workspace` is a real
  directory; `sys.path` does the rest.
- Process model: the supervisor keeps a warm runner (heavy imports
  loaded — this is what the warmed snapshot captured) and **forks it
  per exec**: CoW-hot pandas, fresh globals, SIGKILL on timeout, no
  cross-call module-state leaks. The script model, implemented
  literally.

### Prints and budget-aware rendering

sandtrap's `snapshot_prints` captures raw print objects so the host can
render them sized to the observation budget. The objects can't cross
the boundary — so the budget and renderer move instead:

- The observation budget rides in the exec request; the runner does
  today's per-type smart rendering (DataFrame head/tail, array
  shape+dtype) **guest-side**, where the objects live.
- `prints` becomes a structured stream: each entry rendered at a
  per-entry cap, tagged with metadata (`type`, `shape`/`len`). The host
  composes the final observation within the total budget by allocating
  across entries — per-object intelligence guest-side, cross-entry
  allocation host-side. (The Jupyter split: repr at the kernel, MIME
  bundles on the wire, client composes.) Entries ride the same `Value`
  codec as outputs.
- Entries exceeding their cap spill full renderings to a file and carry
  a `file` ref — "show me more" is a read, not a re-execution.

Rejected: holding the interpreter alive for a `render_more(id, budget)`
RPC. It makes VM lifetime a correctness concern again — trading the
script model's dividend for a nicer expand button. (Note live prints
was always an `isolation="none"` luxury; sandtrap's process mode
already serializes across its boundary and degrades unpicklable
objects. dud forces the general solution rather than introducing the
problem.)

## The apps loop: virtual curl becomes real curl

nontainer's app authoring loop rests on two pieces of stagecraft born
of the no-listener machine: a `curl` builtin that is really
`dispatch()` in a trenchcoat, and test_app's Playwright interception
making the workspace the origin. Under dud:

- **The curl misdirection retires.** Even with `network=False` the
  guest has loopback (no NIC = no egress; `lo` exists). The apps extra
  ships a ~50-line forwarder into the guest: a `localhost` listener
  that wraps HTTP into `hostcall("app.dispatch", request)`. Dispatch —
  routing, per-workspace lock, budgets, read-only-GET rule, logs —
  stays host-side ("one core function, N consumers"; the listener is
  merely a fourth consumer), and handlers execute via `exec_python`
  back in the same guest. Real curl works. So does httpx, wget,
  anything — the agent's whole trained HTTP vocabulary, not one
  blessed builtin. Not a dud feature: dud stays dumb; the forwarder is
  nontainer's.
- **test_app is unchanged.** Browser, interception, and dispatch are
  all host-side; the guest only appears as "where exec_python runs."
  The synthetic-prefix relocatability check survives untouched.
- **Wrinkle — structural REST needs mounts, not wrappers.** Read-only
  GET views (`ReadOnlyFS` today) become a bind-mount-RO in the forked
  runner's unshared mount namespace; per-request atomicity becomes a
  stacked overlay layer (merge on success, discard on raise). Standard
  guest-root machinery, but real work, and rung-2+ only — the macOS
  subprocess rung has no mount namespaces, so rung 1 enforces the GET
  rule at host-side dispatch (reject the diff) rather than at write
  time.

## Policy collapses to image + egress + budgets

sandtrap's policy answered "what can code touch in *my process*?" With a
VM there is no my-process, and the question decomposes:

| sandtrap concept | dud replacement |
|---|---|
| module registrations / import allowlist | **what's installed** — the image |
| member include/exclude patterns | nothing — nothing in the VM is the host's |
| `allow_network`, per-registration grants | NIC present or not; egress rules at the tap |
| ticks, memory limit, timeout | cgroup RAM, wall-clock kill, disk quota |
| `host_objects` gating | the `hostcall` allowlist — the only fine-grained policy that survives, host-side, where a real boundary wants it |

The image spec *is* the config surface:

```python
Dud(
    image="python:3.12-slim",
    pip=["pandas", "pyarrow", "plotly"],   # the "import policy"
    network=False,                          # the default
    memory_mb=1024, timeout=30,
)
```

Spec hash → cached artifact: OCI pull (skopeo-style, no Docker daemon),
flatten to a rootfs, pip layer (uv), then boot once, import the heavy
modules, and take the **warmed snapshot**. First use builds; every
subsequent session resumes instantly.

Notes:

- Image-as-allowlist is *stronger and coarser* than the import gate: no
  bypass (an absent package is a fact, not a promise), no member-level
  granularity (which only mattered when code shared the host
  interpreter).
- The image also inherits sandtrap's **curation** role — keeping the
  agent's world small and legible — but only while `network=False`.
  Enabling network quietly converts the dependency list from an
  allowlist into a suggestion (`pip install` works). Deliberate knob.
- `network=False` means **no NIC at all** (vsock only): no tap devices,
  no root, no iptables. The entire networking-infra tax is deferred to
  the opt-in case, where user-mode networking (gvisor-tap-vsock) still
  avoids root.

## Backend ladder

Same guest supervisor, same rootfs, same wire protocol on every rung —
only the hypervisor differs. That invariant is what keeps it one
product.

| rung | platform | isolation | role |
|---|---|---|---|
| `subprocess` | any OS | none — guest runtime as a host process in a scratch dir | dev / CI / demo floor; `pip install dud` works everywhere with zero artifacts |
| `vfkit` (Virtualization.framework) | macOS (HVF) | real Linux microVM | local dev with a real boundary |
| `firecracker` | Linux + KVM | microVM + jailer | prod fleet; snapshots, density |

macOS dev is therefore **not mimicry** — rungs 2–3 run identical
guests; only the VMM differs. Skew concentrates entirely in rung 1
(BSD userland: agents write GNU-isms like `sed -i`) — accept it for
dev, note it in the tool primer.

> **VMM choice (stage-4 update).** The plan named `libkrun` for the
> macOS rung — one VMM bridging macOS-dev and Linux-prod. In practice
> libkrun isn't installable on macOS (no Homebrew formula; building it
> on Darwin is painful), so the macOS rung is **vfkit** (Apple's
> Virtualization.framework CLI: bottled, carries the
> `com.apple.security.virtualization` entitlement, native HVF). The
> cost is three VMMs instead of two — but the invariant holds: same
> guest supervisor, same rootfs, same vsock wire protocol; only the
> VMM driver differs. **Boot feasibility is proven** (2026-07-18): from
> this sandboxed environment, vfkit boots an arm64 puipui microVM to a
> login prompt with `PF_VSOCK` registered in the guest kernel. Two
> constraints found: host vsock sockets must sit under a short path
> (macOS `AF_UNIX` `sun_path` ≤ 104 chars), and the guest kernel must
> be uncompressed (`Image`, not `Image.gz`) for the VZ Linux
> bootloader.
>
> **Rootfs pipeline proven (stage 4-2, 2026-07-18).** `dud.images`
> pulls `python:3.12-slim` (arm64) with a dependency-free registry-v2
> client (anonymous token → OCI index → arch manifest → digest-verified
> blobs, cached under `~/.dud/blobs`), flattens the layers with OCI
> whiteout semantics, injects the pure-stdlib `dud` runtime into the
> image's `site-packages`, and emits a root-owned `newc` cpio initramfs
> **entirely in Python** — no skopeo, no `mke2fs`, no touching the
> case-insensitive host FS. Cached by spec hash (image digest + guest
> code + workspace). vfkit then boots that exact 43 MB rootfs:
> `kernel → /init (python shebang) → dud.guest.init mounts + vsock dial
> → Supervisor` — **boot to init to power-off in ~0.45 s**. Findings
> that shape the vfkit backend (4-3):
>
> - **vsock direction is host-listens / guest-connects.** vfkit's
>   `virtio-vsock,port=N,socketURL=unix://…` listens on the *host* unix
>   socket and bridges an inbound guest `connect(cid=2, N)` to it. So
>   the backend `listen()`s on a short-path unix socket before boot; the
>   guest dials out (`dud.guest.init` default `mode=connect`, `cid=2`).
> - **Entropy at preinit.** CPython aborts at interpreter init
>   (`_Py_HashRandomization_Init`) because the microVM has no entropy
>   that early and `/dev` isn't mounted yet. Fix without a rootfs
>   change: pass `PYTHONHASHSEED=0` on the kernel cmdline — the kernel
>   forwards unknown `k=v` tokens to init as environment, and a set
>   `PYTHONHASHSEED` makes CPython skip the startup `getrandom()`. A
>   proper `virtio-rng`/kernel-RNG path is a later polish; this unblocks
>   the rung. (The puipui kernel here lacks the virtio-rng driver.)
>   *Since resolved: the pinned kernel has virtio-rng built in and the
>   cmdline workaround is retired.*
> - **Kernel is a bundled asset, not from the image.** `python:slim`
>   ships no kernel; the rootfs pipeline owns only the rootfs half. The
>   arch-matched uncompressed `Image` (with virtio + vsock) is bundled
>   by dud and reused across every rootfs. *Since formalized: the kernel
>   is a versioned, digest-pinned asset fetched by `dud.kernels` — today
>   the Kata Containers release kernel (6.18, overlayfs + virtiofs +
>   virtio-rng all built in), the same kernel Apple's containerization
>   stack recommends for Virtualization.framework guests.*
> - **Initramfs, not ext4, for the rung.** `cpio.gz` needs only present
>   tooling and sidesteps ext4 creation on macOS; the whole rootfs lives
>   in guest RAM (give the VM ~2 GB). ext4-on-a-disk is the scale path
>   if large images make the RAM cost bite; 4-4's overlay `/workspace`
>   staging is independent of the root medium.
>
> **vfkit backend green (stage 4-3, 2026-07-18).** `dud.backends.vfkit`
> boots the rootfs and the guest supervisor serves the *unchanged* wire
> protocol over vsock; the **full conformance suite (48 tests) passes on
> the VM rung** exactly as on subprocess. The protocol logic now lives in
> a shared `HostSession` base both rungs subclass, so they cannot drift.
> Exact vsock recipe (three wrong guesses cost real boots — recording it):
> the device is `virtio-vsock,port=N,socketURL=<BARE_PATH>` with vfkit's
> **default `listen`** semantics — the *host* listens on the unix socket,
> the *guest* connects to CID 2. Two footguns: `socketURL` must be a bare
> path (a `unix://` scheme gets treated as part of the path, so vfkit
> bridges to the wrong socket and the guest's dial is dropped), and the
> `connect` qualifier is the opposite direction (host→guest). The medium
> is read from `meta.json` (a `_medium_boot_args` seam picks the device
> flags), so ext4/virtiofs stay additive. Timings: steady-state
> boot+vsock-connect ≈ **3 s** (kernel→init is ~0.45 s; the rest is the
> guest retrying its dial until vfkit's bridge is ready), `exec_python`
> ≈ 0.03 s. The kernel resolves from an explicit arg, then `$DUD_KERNEL`,
> then `~/.dud/kernels/<arch>/Image`; absent, the rung fails closed
> (`IsolationUnavailable`). Still open: workspace lands in the initramfs
> RAM dir (4-4 gives it overlay staging), and the ~2.5 s dial retry is
> worth trimming (vfkit readiness signal) later.

> **Freeze/thaw green (stage 5 pooling, 2026-07-19).** Firecracker
> snapshots turn parking into files: `freeze()` sends a guest-acked
> `freeze` verb (sync → ack → close → bounded redial loop), pauses the
> VM, writes vmstate + memory into the rundir, and kills the VMM;
> `thaw()` spawns a fresh VMM over `/snapshot/load` and the guest's
> redial lands on a re-bound listener, then `resync` sets the wall
> clock and replaces the fork template (clones of one snapshot must
> not share PRNG state). Restore mmaps the memory file — resume is
> near constant-time in guest RAM, pages fault in on demand. The
> freeze verb is deliberate ceremony: a bare EOF still powers the
> guest off, so process linkage (no dangling VMs) survives; the one
> new dangling shape — a frozen bundle whose host died — is covered by
> a `frozen` marker holding the owner pid, which the rundir sweep
> honors while the owner lives and reaps once it dies. The pool
> duck-types the posture off `freeze`/`thaw`: vfkit parks hot,
> firecracker parks frozen at zero RAM (invisible to `max_total`), one
> acquire/release contract above both.

sandtrap's fail-closed pattern transplants directly: requesting a rung
the platform can't provide **raises** (`IsolationUnavailable`-style),
never silently degrades; results carry a status describing what took
effect.

## Performance & economics

Measured on nontainer-studio's own venv (the realistic DS image):

- Guest-relevant site-packages ≈ **300 MB** (pyarrow 124, plotly 50,
  pandas 49, matplotlib 28, numpy 24, PIL/fontTools ~27). Playwright
  (133 MB), agno, and the LLM SDKs stay host-side. With Python + a slim
  base: **~500–700 MB rootfs**, one-time, per image flavor.
- Importing the full stack: **0.23 s warm, ~107 MB RSS** → warmed
  snapshot memory file ≈ 200–250 MB per image.

The economics that matter:

- **Rootfs size ⊥ boot time.** Block devices demand-page; a 700 MB
  image boots as fast as a 50 MB one. The real cold tax is *imports* —
  and the warmed snapshot (taken post-import at image build) erases it.
- **Latency budget**: cold boot + imports ≈ 1.2 s (paid at image build
  only) · snapshot resume ≈ 5–20 ms + lazy page-in · first exec after
  resume: +tens of ms of faults from the *host* page cache · steady
  state: vsock round trip ~100 µs.
- **Density**: N sessions resumed from one snapshot mmap the same
  memory file; clean pages (nearly all of pandas) are shared through
  the host page cache. Marginal RAM per session is the CoW dirty delta,
  **~10–30 MB**, not 200 MB.
- **Checkpoint cost**: a kvgit commit (KBs) — unchanged from today.
  This is the number no machine-snapshot system can match: checkpoint
  per *tool call*, hundreds per session, kept forever, for free.

`pip install` mid-session hits the overlay at uv speed (host-shared
wheelhouse via a read-only block device makes it seconds); persistence
means baking an image variant — the one workflow genuinely worse than a
venv edit today.

## Prior art

The "fork a VM fast" primitive is commodity now:

- **[Morph Infinibranch](https://cloud.morph.so/docs/blog/developers)** —
  snapshot/branch/restore whole VMs <250 ms, pitched at agent tree
  search. Proprietary, hosted. Versions the machine.
- **[mitos](https://github.com/mitos-run/mitos)** — the closest
  architecture: Firecracker forking (~27 ms restores) + "versioned,
  forkable agent state independent of any sandbox"; `/workspace`
  hydrates on start, dehydrates to a content-addressed store on
  terminate. Validates the hydrate/dehydrate shape — and requires a
  Kubernetes cluster with KVM node pools. Pre-1.0.
- **[forkd](https://github.com/deeplethe/forkd)** — the fork() primitive
  alone (~100–150 ms CoW branches).
- **CodeSandbox SDK, E2B, Modal, Fly** — persistence, resume, or clone;
  none have a history.

What remains unclaimed, and what this design targets: the
**local-first, pip-installable** version with **semantic history** —
per-call commits you can diff, audit, and three-way merge; forks as
branches (data, not N running VMs you're billed for); the cache KV
versioned in the same atomic commit as the files; the same session
history executable against a laptop subprocess, a prod microVM, or (via
nontainer's other providers) no VM at all.

Because the runtime is commodity, `VMBackend` should stay honest enough
that a **remote driver** (Morph/E2B/mitos-backed) is a plausible later
rung — rent the machine, keep the state model.

## Accepted costs

- **Ticks are gone.** Wall-clock kill + cgroup caps: cruder, actually
  enforced.
- **Host objects flatten to RPC.** Callbacks, streaming, rich returns
  need explicit protocol treatment; policy moves to the hostcall
  allowlist.
- **Eager materialize per session start.** Fine at MBs. The upgrade
  path when it isn't: incremental re-materialize — restore/fork to a
  nearby commit ships the provider's commit-to-commit diff into the
  existing lowerdir, not the whole tree. (kvgit computes these diffs
  natively; this is the one place the coupling deepens profitably.)
- **Dev/prod skew** whenever someone develops on rung 1 and deploys on
  rung 3; plus rung-1 BSD/GNU skew.
- **`outputs` narrows to codec-representable values** (see Outputs).

## Sequencing

- **v0 — no VM code at all**: the `Executor` seam in nontainer + the
  `subprocess` backend + the wire protocol. Runs on macOS today.
  Milestone: nontainer-studio end-to-end on dud-subprocess with real
  bash and real Python. This answers the question that gates
  everything: *does real-fidelity execution improve agent behavior
  enough to matter?*
- **v1 — the machine**: libkrun backend, overlayfs staging, OCI →
  rootfs pipeline, warmed snapshots. All the risky unknowns live here.
- **v2 — the fleet**: firecracker backend, serving pool for published
  apps (stateless read-only dispatch VMs), hardened hostcall codec,
  snapshot lifecycle management.

## Kill criteria

Kept here so the project stays honest. If the studio remains
single-user, localhost, trusted-data, and published apps stay behind
known audiences, the existing emulation stack is sufficient and dud is
a distraction. dud earns its existence when any of these are true:

- serving agent-authored apps to anonymous users,
- untrusted data flowing through agent turns routinely,
- DS workloads hitting the monkeyfs fidelity wall in practice,
- wanting duckdb / polars / torch-class dependencies.

v0 doubles as the cheap experiment: if real-fidelity execution doesn't
measurably improve agent quality, stop there.

## Open questions

- **Incremental materialize protocol** — when to graduate from tarball
  to commit-diff shipping; whether the provider seam needs a
  `diff(commit_a, commit_b)` capability flag.
- **hostcall codec** — msgpack + allowlisted types vs. per-object typed
  stubs; how streaming/callbacks degrade.
- **cache-as-service semantics** — read-your-writes within a call,
  staging interaction, size limits on the wire.
- **Egress design** for `network=True` — gvisor-tap-vsock, allowlist
  format, DNS.
- **GitProvider** — a plain git repo as a `WorkspaceProvider`; nearly
  free given tree-in/diff-out, and a very legible demo.
- **Sub-task delegation as a host service** — guests can't nest VMs
  (Firecracker masks VMX; the DinD lesson applies anyway), and don't
  need to: a host-registered `subtask` service lets an agent request
  "run X on a fork of my workspace; return the branch." The sibling VM
  is an implementation detail the guest never sees — the service is
  just another hostcall registration, implemented host-side by
  composing `ws.fork()` + an executor + a sub-loop. **dud needs zero
  new verbs for this.** Recursion lives in the branch tree; machines
  stay flat under one manager. Policy at the registration, as always:
  max concurrent sub-tasks, image allowlist, budget subdivision. Also
  the worked example that hostcall subsumes what in-process nontainer
  needed three mechanisms for (host_objects, cache, and now
  delegation): named services, typed codec, host-side allowlist.
- **Per-blob content addressing in kvgit** (currently `{commit}:{key}`,
  write-once but not content-addressed) — not a blocker, but at a VM
  trust boundary it buys put-verification (`sha(bytes) == key`), upload
  skipping, and cross-session dedup. Candidate storage v4.
