# Roadmap

What isn't built yet.

- [README.md](README.md) — what it is, how to use it, what it costs.
- [CHANGELOG.md](CHANGELOG.md) — what shipped, and when.
- [DESIGN.md](DESIGN.md) — why it's shaped this way, including
  ["Roads not taken"](DESIGN.md#roads-not-taken): the things considered
  and declined, so they don't get re-litigated here.

All three rungs are live and pass the same conformance corpus; what
that covers is the CHANGELOG's business rather than this file's.

## Open

### Firecracker backend

- **Golden snapshots per boot fingerprint** (the identity a pooled VM
  is keyed by: image, memory, medium, scratch) — boot once, freeze
  clean, then every pool miss thaws a clone instead of cold-booting.
  The remaining half of the parking story.
- **Hardening** — jailer, cgroup budgets. The production-grade wrapper;
  not needed for local development.
- **amd64 pins** (kernel `vmlinux`, debs) — wanted for CI anyway, since
  GitHub's `ubuntu-latest` runners have `/dev/kvm`, so firecracker
  conformance could run in plain hosted Actions on x86-64.

### Serving

Today an "app" is inseparable from its authoring session — every
GET/POST is an exec in the session's own VM, serialized with the agent's
calls. Serving splits that:

- **Read-only dispatch pool**: N VMs booted from a *published* kvgit
  snapshot, GET-only dispatch, no writer, disposable or warm-pooled.
  Inherits the view-worker machinery wholesale.
- **In-guest serving / real `curl`**: the remaining agent-loop gap. The
  terminal `curl` is a termish builtin; real bash in the guest has
  neither the binary nor a server to hit. Either an in-guest forwarder
  (see DESIGN, "The apps loop") or a wire-level shell→host bridge.
  Agents use `test_app`/preview meanwhile.
- **hostcall codec hardening** — a prerequisite, not a nice-to-have;
  see Design questions below for the shape of it.

### CI matrix

Conformance runs per-rung locally (`DUD_BACKEND=vfkit`). CI needs:
subprocess everywhere (works today); vfkit on macOS arm64 — open
question whether GitHub's hosted runners allow
Virtualization.framework; may need a self-hosted Mac. Firecracker needs
a KVM-capable Linux runner. **Golden transcripts** — pinning
agent-visible runner semantics (echo, harvest, print caps, error
rendering) across rungs in one corpus — become valuable exactly here.

### Workspace fidelity

- **Symlinks don't round-trip.** They survive inside a session and no
  longer vanish at checkpoint, but a link an agent creates never
  reaches the consumer. Carrying relative, inside-workspace targets is
  the easy and useful half. Absolute ones (`venv/bin/python3.13 ->
  /usr/local/bin/python3.13`) are the decision: the stdlib's `data`
  extraction filter refuses them outright, and an image-absolute link
  arguably isn't workspace *state* at all — it only means anything in
  that exact image. Whatever the disposition, record what was dropped
  rather than dropping it silently, the way `outputs_skipped` does.
- **Empty directories don't round-trip.** Files imply their parents.
  Probably leave it: git doesn't track them either, so a git-shaped
  provider has nowhere to put one.

### Reach

- **The `python:*-slim` requirement.** `rootfs.py` demands an
  interpreter at `/usr/local/bin/python3` and a matching
  `site-packages` layout, which is narrower than "has Python". Relaxing
  it to *find* an interpreter is the first rung of the language-neutral
  ladder in DESIGN's thesis, and the only one that pays off without
  protocol work.

### Loose ends

- **Boot latency**: ~2.5 s of the 3 s initramfs boot is the guest
  retrying its vsock dial until the VMM's bridge is ready (erofs boots
  in ~1 s). Find or add a readiness signal. Matters less now that pool
  reuse skips boots and recovery is rare, but still paid on pool misses
  and first opens.
- **Studio still defaults to initramfs**; flipping it to `medium="auto"`
  is a one-arg decision, worth bundling with eager-warming work.
- **No side-by-side numbers against sandtrap.** nontainer-studio
  harnesses run against dud and the substrate carries real agent loops,
  so this is no longer a question of whether the approach works — but
  the same loop has never been run both ways with numbers worth
  quoting. Worth having whenever a claim needs backing; blocking
  nothing. (An older note here pointed at a `dogfood/analyst_agent.py`
  in this repo that never existed — the harnesses live in the studio.)

### Design questions

Carried over from DESIGN, which should say why things are shaped as
they are rather than track what hasn't been decided.

- **Incremental materialize protocol** — when to graduate from tarball
  to commit-diff shipping; whether the provider seam needs a
  `diff(commit_a, commit_b)` capability flag.
- **hostcall codec hardening** — the JSON + allowlisted-types codec vs.
  per-object typed stubs; how streaming and callbacks degrade. Wanted
  before anything serves untrusted traffic.
- **cache-as-service semantics** — read-your-writes within a call,
  staging interaction, size limits on the wire.
- **Egress design**, if network is ever wanted — gvisor-tap-vsock,
  allowlist format, DNS.
- **GitProvider** — a plain git repo as a `WorkspaceProvider`; nearly
  free given tree-in/diff-out, and a very legible demo.
- **Sub-task delegation as a host service** — guests can't nest VMs
  (Firecracker masks VMX; the DinD lesson applies anyway), and don't
  need to: a host-registered `subtask` service lets an agent request
  "run X on a fork of my workspace; return the branch." The sibling VM
  is an implementation detail the guest never sees — the service is just
  another hostcall registration, implemented host-side by composing
  `ws.fork()` + an executor + a sub-loop. **dud needs zero new verbs for
  this.** Recursion lives in the branch tree; machines stay flat under
  one manager. Policy at the registration, as always: max concurrent
  sub-tasks, image allowlist, budget subdivision. Also the worked
  example that hostcall subsumes what in-process nontainer needed three
  mechanisms for (host_objects, cache, and now delegation): named
  services, typed codec, host-side allowlist.
- **Per-blob content addressing in kvgit** (currently `{commit}:{key}`,
  write-once but not content-addressed) — not a blocker, but at a VM
  trust boundary it buys put-verification (`sha(bytes) == key`), upload
  skipping, and cross-session dedup. Candidate storage v4.
