# dud 🧨 — design notes

*A dumb firecracker.* Real, disposable microVMs as an execution
substrate for [nontainer](https://github.com/ashenfad/nontainer)-style
versioned workspaces — `pip install dud`, no Kubernetes, no daemon, no
cloud.

> **Scope.** This is the *why*: the thesis, the decisions that shaped
> what got built, the costs knowingly accepted, and the roads not
> taken. It deliberately does not track status — what shipped and when
> is [CHANGELOG.md](CHANGELOG.md), what isn't built is
> [ROADMAP.md](ROADMAP.md), and what it is and costs to run is
> [README.md](README.md).

## Thesis

nontainer's stack virtualizes **state, not the machine**: the workspace
is versioned, forkable, content-durable data (kvgit), and the "computer"
the agent uses is an emulation good enough to work against that state —
termish for the shell, monkeyfs for the filesystem, sandtrap for gated
Python.

dud is the observation that the state layer never depended on the
machine being fake. Because nontainer uses a **script model** (no
resident REPL — state lives in files + cache, never in a live heap), the
machine is stateless between calls. So a *real* machine — a microVM
booted from a real Linux image — slots in as the executor, and:

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

That third point has a corollary worth stating plainly, because it
outlives the fidelity argument: the emulation stack tops out at what
Python semantics can express. sandtrap gates Python ASTs, monkeyfs is a
Python VFS, termish emulates a shell — a second language means
rebuilding all three. A machine has no such ceiling. Whatever makes C
extensions work is the same thing that makes node work; reach is not a
feature that gets added twice.

dud can't collect on that yet, and the distance divides into three
levels worth keeping apart. Running a non-Python *program* is a
build-time question only: `exec_shell` is already language-blind — real
bash, real binaries, diff out — so it wants nothing but an image, and
the only thing in the way is `rootfs.py` requiring a `python:*-slim`
layout. Giving a second language the *runner* contract (harvested
outputs, cache, host proxies, last-expression echo) costs more than a
rootfs: `exec_python` is a verb on the wire, the supervisor spawns
`sys.executable -m dud.guest.runner` by name, and the warm fork
template is Python's too. That is protocol and dispatch work — additive
rather than a redesign, since a runner is just a process speaking JSON
over a socketpair and a second one would speak the same — but it is
real work on both sides of the boundary. Only the third level is
architectural: dropping the interpreter requirement altogether means a
supervisor that isn't Python.

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

1. **`WorkspaceProvider`** — fs + kv + versioning verbs, gated by
   capability flags. Unchanged by dud.
2. **`Executor`** — execution used to be welded to sandtrap inside
   `Workspace`. dud forced the seam out, the same way AgentFS forced
   `WorkspaceProvider`. sandtrap is now the default `LocalExecutor`;
   `DudExecutor` is the second implementation. The agent-facing tool
   surface (`terminal`, `run_python`, cache, checkpoints on results) is
   identical across executors.

```
Executor:
    open(tree: TreeSource) / close()
    exec_shell(script, env, cwd)   -> ShellResult
    exec_python(code, inputs)      -> ExecResult      # runner-based; see Outputs
    diff()                         -> StagedDiff      # harvested at checkpoint
    reset()                                           # discard staged writes
```

## Execution model

The workspace mounts in the guest **at the configured root**
(`/workspace` by default) as overlayfs: lowerdir = the materialized
snapshot (read-only), upperdir = the staging area. The upperdir *is*
kvgit's staged buffer, implemented by the kernel:

- `diff()` harvests the upperdir (with whiteouts for deletes) — an exact
  staged diff, no mtime scanning;
- `reset()` wipes the upperdir — `discard()` semantics for free;
- nontainer's atomicity rule ("a handler that writes three files then
  raises leaves nothing behind") survives as mount-level operations.

The merged mount lives at the root itself, not a subdirectory of it, and
the backing trees (snapshot, upper, overlay work) live on a separate
tmpfs outside the root. Two things follow, and both are the point:
absolute writes like `/workspace/x` land in the upper and ride the diff
like any other write, and the agent-visible root contains exactly the
workspace. The backing trees being out of guest reach is not just
tidiness — mutating a mounted overlay's lower or upper dir is
kernel-documented undefined behavior.

That root is a shared contract, not a dud detail. nontainer presents its
local VFS at the same path, so an absolute path in agent code names the
same file whether it executes in-process or in a VM. `/workspace` rather
than `/` because a guest can't mount a workspace at `/` (that's the
rootfs), a VFS can present any prefix it likes, and `/workspace` is by
far the strongest training prior in the agent ecosystem.

Session lifecycle exploits the script model: the VM is a **pure
performance artifact**. Warm while the session is active; killed when
idle (nothing is lost — state is in the provider); re-created against
the same tree on the next turn. Fork = provider branch (O(1)) + fresh VM
pointed at it. Restore = re-materialize a different commit. VM lifetime
is a tuning knob, never a correctness concern.

That principle generalizes into the one that governs the whole
lifecycle: **death recovery is the primitive, not a resilience
feature.** Once any VM can vanish at any moment and its owner recovers
uniformly — re-acquire, re-push, retry once — idle eviction and capacity
pressure stop being separate mechanisms. They are just deliberate
deaths.

## The three planes

A guest sees three planes, with progressively weaker guarantees and
progressively cheaper writes:

| plane | where | guarantee |
|---|---|---|
| **state** | the workspace | committed, versioned, the *complete* description of a session |
| **cache** | the kv `cache` | managed, transactional (writes land only on successful execs) |
| **scratch** | `/tmp` | best-effort memoization; may vanish at any moment |

The scratch plane formalizes what was previously "out-of-workspace
residue": a writable ext4 volume mounted over `/tmp`, whose contents are
**cache in the strict sense** — derived, re-derivable, and droppable
without correctness consequences. The commit remains the complete
guarantee of state *because* scratch is structurally excluded from it:
scratch never appears in diffs, never enters commits, is never restored,
and a fork always starts cold. Anything an app must not lose does not
belong in `/tmp`.

The intended customer is a **published app** (a frozen snapshot): its
commit never advances, so a scratch keyed to it can never go stale —
materialize an index on the first query, serve every later query fast.
Interactive sessions get VM-lifetime scratch only. Their commit moves
every checkpoint, and versioning scratch against history would blur the
state guarantee that makes commits meaningful.

Mechanically, a blank sparse ext4 **master** is baked once per size
class. Each boot attaches a CoW **clone** of the caller's master and
mounts it over `/tmp` — nothing is copied, blocks materialize as
touched. On a *clean* park or shutdown the clone is promoted back to the
master (clonefile + atomic rename; last-clean-wins — it's cache). A
crashed VM's clone is discarded with its rundir, and the ext4 journal
means a promoted-mid-flight volume mounts clean in-kernel, no guest
fsck. The master's path is part of the VM's **boot fingerprint** — the
identity a pooled VM is keyed by, covering everything baked in at boot
(image, memory, medium, scratch) — so a
pooled VM only ever serves sessions keyed to the same scratch — no
cross-key cache leakage through reuse.

Two properties keep the contract honest. First, the disposable thesis
*polices* it: VM death, pool reclaim, and eviction clear scratch
constantly and randomly, so code that secretly depends on it breaks in
development, not production. Second, scratch is a shared surface within
its key — handlers serving multiple viewers must not stash per-viewer
secrets in `/tmp`, the same rule as any server-side cache.

## Wire protocol

One channel between host and guest supervisor (vsock for VMs, unix
socket for the subprocess backend), with services multiplexed on it.
A frame is a length-prefixed **JSON** header plus a count of raw binary
attachments — structure where structure helps, bytes where they don't.
(This document said msgpack for a long while; it never was.)

Anything unbounded rides the binary side: workspace tars, diff tars,
and cache values. Base64 in the header would be a 1.33x string encoded
by the sender, parsed by the supervisor forwarding it, and decoded by
the receiver — three transient copies of a payload that was never text.
Measured on an 8 MB cache value, moving it off base64 took the round
trip from ~108 ms to ~47 ms in each direction. Small typed values stay
in the header, where being able to read a frame is worth more than the
bytes it costs.

| verb | direction | purpose |
|---|---|---|
| `push_tree` | host → guest | materialize the workspace (tar) |
| `exec_shell` | host → guest | run script; returns transcript, exit, new cwd/env |
| `exec_python` | host → guest | run code via the Python runner |
| `pull_diff` | host → guest | harvest upperdir + whiteouts |
| `reset_stage` | host → guest | wipe upperdir |
| `emit` | guest → host | fire-and-forget structured output (see Outputs) |
| `cache.get/put` | guest → host | the KV plane as a service |
| `hostcall` | guest → host | invoke an allowlisted host-object method |
| `freeze` / `resync` | host → guest | snapshot parking (see Backend ladder) |
| `ping` / `shutdown` | host → guest | lifecycle |

**No pickle ever crosses this boundary.** Cache values are pickled and
unpickled only guest-side; the host stores opaque bytes. The only
host-side deserialization surfaces are `hostcall` arguments and `emit`
payloads, both restricted to a typed codec (JSON over an allowlisted
type set, with `bytes` base64'd inside it — these are specced small; a
large payload belongs in the workspace as a file). This closes, by construction, the worker→host pickle hole
documented in sandtrap's threat model — the boundary is only as strong
as this rule.

## Outputs: emits, not namespaces

`result.namespace` — sandtrap's harvest of top-level bindings — was the
one Python-specific concept in the old contract, and it assumed live
objects could reach the host. Both assumptions die here. The script
model makes this painless: namespace was never *state* (state is cache +
helpers + files), only a per-call return channel. So the channel gets a
name:

```
ExecResult { transcript, exit_code, outputs: {name → Value} }

Value = json(...)                  # the floor
      | bytes(mime, data)          # small binary
      | file(path)                 # ref into the workspace — lands in the commit
      | chart(spec) | table(spec)  # declarative UI (plotly JSON, columnar)
```

Population is per-runtime sugar over the language-neutral `emit` service
(a well-known channel in the exec environment — **not stdout markers**,
which agent code echoing untrusted data could forge). The Python runner
harvests top-level bindings post-exec and emits what's
codec-representable, offering the whole harvest first to a hook the
caller names (`session(outputs_hook="pkg.module:function")`) and the
*image* provides. A `ui = {...}` convention still works, but as
something the hook implements rather than something dud knows: which
objects flatten, into what shape, under what path, and which binding
collects them, all belong to whoever consumes the diff. dud's part is
to offer the values and report whether the named hook resolved —
host-side config rather than a well-known module name, because dud
naming *reprobate* is dud depending on a library's API, where naming a
`dud_outputs` would be dud claiming a module namespace on the
consumer's behalf. A future Node runner would harvest
`exports` or explicit `emit()` calls — an image plus a small runner, no
core changes. Bash gets a `dud-emit` CLI or writes files and returns
refs.

**Forcing function**: the emit channel is specced so bash can use it.
Bash has no namespace, no objects, no pickle — if the contract is
ergonomic from bash, it's language-neutral by construction.

**Accepted loss**: arbitrary live Python values out of `run_python`.
Embedders who need rich values back use `file` refs and deserialize
host-side as an explicit, trust-aware act — never an ambient one.

### The Python runner

sandtrap did two jobs; only one needed its machinery. **Policy** (AST
rewriting, gates, builtins whitelist) is replaced by the VM. **Plumbing**
(namespace injection, cache, harvest, echo) is ordinary Python:

- Build a globals dict: decoded `inputs` as bindings; `cache` as a
  read-through/write-back view over the channel; each host-object
  registration as a **dumb proxy** whose `__getattr__` returns a
  hostcall-marshalling callable. Proxies are safe *by construction*:
  there is no real object in guest memory to traverse to — agent code
  can only produce hostcalls, which the host validates against the
  registration allowlist.
- Plain `exec` — no rewriting. Last-expression echo via a small `ast`
  split (the runner's only ast use; the same trick as Jupyter's
  `last_expr`). `print` shadowed for raw-object capture into the prints
  stream.
- Harvest top-level bindings post-exec (minus injected names and
  underscores) into the `Value` codec.
- Cache writes buffer guest-side and flush **on successful completion
  only** — atomic with the call's checkpoint, matching staging semantics
  (a raise leaves nothing behind, cache included).
- `helpers/` needs no VFS import hook: the workspace is a real
  directory; `sys.path` does the rest.

Process model: read-only **view execs** fork from a warm import template
— the template imports the image's packages once at boot, never runs
user code, and forks per request, so a view gets CoW-hot pandas with a
genuinely fresh namespace and the child exits after one request. Regular
execs spawn. The asymmetry is deliberate: views are the latency-visible
path (a preview GET blocks a human), where regular execs are already
amortized against an LLM round trip.

### Prints: raw material, not an observation budget

sandtrap's `snapshot_prints` captures raw print objects so the host can
render them sized to the observation budget. The objects can't cross the
boundary, so what dud ships instead is a structured stream:

- `prints` is a list of entries, each carrying the printed text plus
  metadata the text alone would lose (`type`, and `shape`/`len` where
  the object had one). Entries ride the same `Value` codec as outputs.
- `caps` in the exec request are **resource guards, not a budget**: a
  per-entry ceiling, an entry-count ceiling, a total across entries, and
  one on the transcript. They exist so a runaway print loop can't flood
  vsock or balloon a supervisor that is PID 1 — the same class of thing
  as the wall-clock kill and the RAM cap at boot. They are set high
  enough not to be a policy in disguise.
- **Deciding what the model should see is the host's job.** dud has no
  business ranking an agent's output, and the layer above already knows
  the observation budget, the model, and the turn. It receives every
  entry with its metadata and allocates across them.

Rendering is the exception, and the reason is physical rather than a
preference: a DataFrame's head/tail cannot be reconstructed from a
chopped string, so it has to happen where the object still exists. The
runner shadows `print` in the exec globals, which puts it holding the
live arguments — the one place in the system that does.

So `render_budget` rides the exec request and the guest applies it via
[reprobate](https://github.com/ashenfad/reprobate), turning
`[0, 1, 2, ...]` into `[0, 1, 2, ...86 more]` rather than a severed
token. The number stays the host's; dud never invents one, and unasked
it ships plain `str()`. That keeps the split intact — **rendering
guest-side because it must be, budgeting host-side because it should
be** — and it closes a real gap: the in-process executor could
re-render from snapshotted objects, so until this existed the VM path
gave an agent strictly worse output than the emulation it replaces.

Two deliberate limits. The **transcript is never rendered**: fidelity
outranks the observation, and `print(df)` must produce what a real
machine produces, so only the structured entry is summarized. And the
renderer is **optional** — dud has no dependencies and the image is the
config surface, so callers layer `packages=["reprobate"]` and `ping()`
reports which renderer is live, on the same reasoning that makes it
report which staging strategy is live: a silent fallback is one tests
pass over.

Coverage follows from the mechanism. Shadowing reaches the exec'd
code's own globals, so an imported module's prints and direct
`sys.stdout` writes reach the transcript but never become entries.
That bounds what any renderer here can see — the agent's own top-level
prints and the echo, which is where `print(df)` actually lives.

Rejected: holding the interpreter alive for a `render_more(id, budget)`
RPC. It makes VM lifetime a correctness concern again — trading the
script model's dividend for a nicer expand button. (Live prints was
always an `isolation="none"` luxury; sandtrap's process mode already
serializes across its boundary and degrades unpicklable objects. dud
forces the general solution rather than introducing the problem.)

## Policy collapses to the image

sandtrap's policy answered "what can code touch in *my process*?" With a
VM there is no my-process, and the question decomposes:

| sandtrap concept | dud replacement |
|---|---|
| module registrations / import allowlist | **what's installed** — the image |
| member include/exclude patterns | nothing — nothing in the VM is the host's |
| `allow_network`, per-registration grants | no NIC exists; vsock is the only channel |
| ticks, memory limit, timeout | RAM/CPU at boot, wall-clock kill |
| `host_objects` gating | the `hostcall` allowlist — the only fine-grained policy that survives, host-side, where a real boundary wants it |

Being the only surviving fine-grained policy is what makes that
allowlist mandatory rather than optional. It gates a *live host
object* — the one thing on this boundary that isn't disposable — so an
absent entry raises (`PolicyError`) instead of granting every public
method. It shipped permissive at first, on the rung-1 reasoning that
the caller and the agent were the same person; that stopped being true
the moment a VM rung existed, and a default nobody types is the one
most deployments run. Fail-closed here matches `IsolationUnavailable`:
the parts of this system that answer "what may this code reach" do not
guess.

So the image spec *is* the config surface: a base ref, layered pip
packages (prebuilt guest-arch wheels), layered pinned debs for system
tools, and a medium. Spec hash → cached artifact, built once and reused.

Image-as-allowlist is *stronger and coarser* than an import gate: no
bypass (an absent package is a fact, not a promise), and no member-level
granularity — which only ever mattered when agent code shared the host
interpreter. The image also inherits sandtrap's **curation** role,
keeping the agent's world small and legible.

Guests have **no network interface at all** — not a disabled one. There
is no egress to configure, no tap device, no iptables, no root
requirement, and the entire networking-infra tax is deferred to a
hypothetical opt-in future (where user-mode networking via
gvisor-tap-vsock would still avoid root). This is why `pip install`
doesn't work inside a running guest and packages have to be layered at
build time — a real constraint, accepted deliberately, and the reason
curation survives: enabling network would quietly convert the dependency
list from an allowlist into a suggestion.

## Backend ladder

The backends are ordered by how strong a boundary they put around agent
code — a ladder, not a set of interchangeable peers — and this project
calls a step on it a **rung**. (The API calls them `backend=`; these
docs say "rung" where the ordering is the load-bearing part.)

Same guest supervisor, same rootfs, same wire protocol on every rung —
only the hypervisor differs. That invariant is what keeps it one
product, and the conformance suite enforces it: a backend that can't
pass the corpus unchanged isn't a rung.

| rung | backend | platform | isolation | role |
|---|---|---|---|---|
| 1 | `subprocess` | any OS | none — guest runtime as a host process in a scratch dir | dev / CI / demo floor; `pip install dud` works everywhere with zero artifacts |
| 2 | `vfkit` (Virtualization.framework) | macOS (HVF) | real Linux microVM | local dev with a real boundary |
| 3 | `firecracker` | Linux + KVM | microVM + snapshot parking (jailer pending) | prod fleet; density |

macOS dev is therefore **not mimicry** — the two VM rungs run identical
guests; only the VMM differs. Skew concentrates entirely in
`subprocess` (BSD userland: agents write GNU-isms like `sed -i`; and
the workspace can't claim an absolute root, since a host temp dir isn't
`/workspace`). Accept it for dev, note it in the agent's tool
instructions.

The macOS rung is vfkit rather than the originally-planned libkrun,
which would have bridged macOS-dev and Linux-prod with one VMM: libkrun
isn't practically installable on macOS (no Homebrew formula, painful to
build on Darwin). The cost is three VMMs instead of two, and the
invariant absorbed it without complaint.

The guest kernel is a versioned, digest-pinned **asset**, not something
extracted from the image — `python:slim` ships no kernel, and the rootfs
pipeline owns only the rootfs half. It's the Kata Containers release
kernel, which has overlayfs, virtiofs, and virtio-rng all built in, and
is what Apple's own containerization stack recommends for
Virtualization.framework guests.

**Snapshot parking** (firecracker) turns a parked VM into files: a
guest-acked `freeze` verb, then pause, then vmstate + memory written to
the rundir, then the VMM dies. `thaw()` spawns a fresh VMM over the
snapshot, the guest redials, and `resync` fixes the wall clock and
replaces the fork template — clones of one snapshot must not share PRNG
state. Restore mmaps the memory file, so resume is near constant-time in
guest RAM with pages faulting in on demand.

The freeze verb is deliberate ceremony. A bare channel EOF still powers
the guest off, which is what preserves the **no-dangling-VMs
invariant**: VM lifetime is process-linked, the VMM exits with its
guest, so even a `kill -9` of the host cascades to full cleanup with no
orphan reaper. Only an acked freeze enters the guest's bounded redial
loop. The one new dangling shape — a frozen bundle whose host died — is
covered by a marker holding the owner pid, which the rundir sweep honors
while the owner lives and reaps once it dies.

Above both sits one pool contract: vfkit parks hot, firecracker
parks frozen at zero RAM, same acquire/release, same affinity tags.

sandtrap's fail-closed pattern transplants directly: requesting a rung
the platform can't provide **raises** (`IsolationUnavailable`), never
silently degrades.

## The apps loop: virtual curl becomes real curl

nontainer's app authoring loop rests on two pieces of stagecraft born of
the no-listener machine: a `curl` builtin that is really `dispatch()` in
a trenchcoat, and test_app's Playwright interception making the
workspace the origin. Under dud:

- **The curl misdirection can retire** — *not yet built; see ROADMAP.*
  Even with no NIC the guest has loopback (`lo` exists; no NIC means no
  egress, not no localhost). A small forwarder in the guest — a
  `localhost` listener wrapping HTTP into `hostcall("app.dispatch",
  request)` — makes real curl work, and httpx, and wget: the agent's
  whole trained HTTP vocabulary rather than one blessed builtin.
  Dispatch itself (routing, per-workspace lock, budgets,
  read-only-GET rule, logs) stays host-side — "one core function, N
  consumers", the listener merely being a fourth consumer — and handlers
  execute via `exec_python` back in the same guest. Not a dud feature:
  dud stays dumb, the forwarder is nontainer's.
- **test_app is unchanged.** Browser, interception, and dispatch are all
  host-side; the guest only appears as "where exec_python runs." The
  synthetic-prefix relocatability check survives untouched.
- **Read-only GET views are a real mount**, not a wrapper. On VM rungs a
  view exec gets a genuine read-only remount of the workspace, so the
  GET rule is enforced at write time by the kernel. `subprocess` has no such
  machinery and enforces it post-hoc at host-side dispatch (reject the
  diff) — a real gap, accepted because that backend is for development.

## Performance & economics

Measured numbers live in the [README](README.md). What's worth
recording here is the shape behind them, because it's what makes the
approach viable rather than merely clever:

- **Rootfs size ⊥ boot time.** A read-only erofs root demand-pages from
  the host page cache; a 700 MB image boots like a 50 MB one, and guest
  RAM is proportional to pages actually touched rather than to image
  size. The real cold tax was never the rootfs — it's *imports*, which
  is why the warm fork template exists.
- **Diffs are O(changes), not O(tree).** The overlay upper *is* the
  diff. This is the difference between a workspace that scales and one
  that quietly gets slower as an agent works in it.
- **Density**: VMs restored from the same memory file share clean pages
  through the host page cache. Marginal RAM per session is the CoW dirty
  delta, not the whole footprint.
- **Checkpoint cost**: a kvgit commit (KBs). This is the number no
  machine-snapshot system can match — checkpoint per *tool call*,
  hundreds per session, kept forever, for approximately free.

The economics that don't work: `pip install` mid-session (no network —
persistence means baking an image variant, the one workflow genuinely
worse than a venv edit today), and eager tree materialization at session
start, which is fine at MBs and would need incremental commit-diff
shipping if it ever isn't.

## Prior art

The "fork a VM fast" primitive is commodity now:

- **[Morph Infinibranch](https://cloud.morph.so/docs/blog/developers)** —
  snapshot/branch/restore whole VMs <250 ms, pitched at agent tree
  search. Proprietary, hosted. Versions the machine.
- **[mitos](https://github.com/mitos-run/mitos)** — the closest
  architecture: Firecracker forking (~27 ms restores) + "versioned,
  forkable agent state independent of any sandbox"; the workspace
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

Because the runtime is commodity, the backend seam should stay honest
enough that a **remote driver** (Morph/E2B/mitos-backed) is a plausible
later rung — rent the machine, keep the state model.

## Accepted costs

- **Ticks are gone.** Wall-clock kill + resource caps at boot: cruder,
  actually enforced.
- **Host objects flatten to RPC.** Callbacks, streaming, and rich
  returns need explicit protocol treatment; policy moves to the hostcall
  allowlist.
- **No network in the guest.** Packages are a build-time decision.
- **Eager materialize per session start.** Fine at MBs. The upgrade path
  when it isn't: incremental re-materialize — restore/fork to a nearby
  commit ships the provider's commit-to-commit diff into the existing
  lowerdir, not the whole tree. (kvgit computes these diffs natively;
  this is the one place the coupling deepens profitably.)
- **Dev/prod skew** whenever someone develops on `subprocess` and
  deploys on `firecracker`, plus that backend's BSD/GNU and
  absolute-path skew.
- **`outputs` narrows to codec-representable values.**

## Roads not taken

Decisions made against, with the reasoning, so they don't get
re-litigated. What is merely *unbuilt* lives in
[ROADMAP.md](ROADMAP.md); these are the things that were considered and
declined.

- **Restart survival (detached VMs)** — decided against 2026-07-19. VM
  lifetime is process-linked *as an invariant*: channel EOF powers the
  guest off, the VMM exits with its guest, so even a `kill -9` of the
  host cascades to full cleanup with no orphan reaper. Detaching removes
  exactly that safety and rebuilds its guarantees by hand (socket
  rediscovery, adopt protocols, stale-guest-runtime handshakes) to save
  a ~1 s boot whose tree would be re-pushed from kvgit anyway. Where
  durable warmth actually matters — the firecracker rung — it arrives
  structurally instead: snapshots are files, and files survive restarts
  without any process outliving anything.
- **virtiofs lowerdir** (host-mounted workspaces instead of tarring over
  vsock) — deferred indefinitely 2026-07-19. The scratch plane routes
  bulk to disk cache, so state stays small — kvgit's own assumption —
  and eager hydration (~0.4 s / 200 MB) stops mattering. Revisit only if
  large *source* data (uploads that are genuinely state) becomes common.
- **OS-level sandboxing around the subprocess backend**
  (Seatbelt/Landlock — a half-step between "no isolation" and a real
  VM) — the vfkit backend landing on macOS removed most of its
  audience. Revisit only if a Linux-dev-without-KVM constituency
  appears.
- **Per-blob content addressing** — kvgit's concern (storage v4
  candidate), not dud's.
- **Windows** — no rung, no plan.
- **Incremental tree push** — `push_tree` wipes and re-extracts on
  restore; fine at current workspace sizes.
