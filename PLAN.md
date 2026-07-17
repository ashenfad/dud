# Implementation plan

Staged so that every stage lands standalone value, the riskiest work is
deferred behind a go/no-go gate, and the backend ladder's invariant
(same guest contract on every rung) is enforced by a shared conformance
suite from day one. See [DESIGN.md](DESIGN.md) for rationale.

## Decisions to settle before code

Small, but each blocks a stage-0 interface:

1. **Diff wire format** — one encoding that both producers emit:
   overlayfs upperdir harvest (rungs 2–3) and content-hash scan
   (rung 1 — macOS has no overlayfs; scan-diff against the materialize
   index is fine at workspace scale). Proposal: tar of
   changed/added files + explicit delete list; no whiteout encoding on
   the wire.
2. **exec_shell persistence contract** — cwd/env survive across calls
   (script model applies to *Python* state; shell working state is
   workspace-adjacent). Proposal: supervisor wraps each script, dumps
   final cwd/env to a side fd, replays into the next call. `$?`
   semantics per termish precedent.
3. **Value codec v0** — minimum set: `json`, `bytes(mime)`, `file`.
   `chart`/`table` ride as `json` with a convention key until proven
   worth first-classing.
4. **Executor protocol signatures** — freeze the sketch in DESIGN.md
   (open/close, exec_shell, exec_python, diff, reset) including error
   taxonomy (guest crash vs code error vs timeout).
5. **Runner exec model on rung 1** — fork-per-exec (CoW warm imports)
   vs spawn-per-exec (slower, no fork-safety caveats on macOS).
   Proposal: fork on Linux rungs, spawn on macOS rung 1; the runner is
   single-threaded so fork is likely fine, but rung 1 is not the rung
   to fight fork-safety on.

## Stage 0 — wire protocol + guest runtime (dud standalone)

No nontainer involvement. Deliverables:

- msgpack framing, request ids, protocol version field from day one.
- Supervisor: `push_tree` / `exec_shell` / `exec_python` / `pull_diff` /
  `reset_stage` / `ping` / `shutdown`; `emit` / `cache.*` / `hostcall`
  loop back to a test harness.
- Python runner: globals build (inputs, CacheView, HostProxy),
  plain exec, last-expression echo, harvest, structured prints with
  per-entry caps + spill refs.
- Subprocess backend: supervisor as a child process over a unix
  socket, scratch-dir workspace, scan-diff.
- **The conformance suite** — backend-agnostic tests speaking only the
  wire protocol. Every later backend runs this unchanged.

Exit: `dud` pytest suite green; a demo script that pushes a tree, runs
bash + python against it, pulls a diff.

## Stage 1 — the Executor seam (nontainer refactor, no dud)

Pure refactor: extract the Executor protocol from `Workspace`;
sandtrap becomes `LocalExecutor`. Zero behavior change.

Exit: existing nontainer test suite green with no test edits. This is
the stage that proves the seam is real, and it lands value (a cleaner
nontainer) even if everything after is abandoned.

## Stage 2 — DudExecutor (subprocess) wired into nontainer

- Tree export: generic tar-from-`FileSystem`-protocol helper (no
  provider changes needed); diff import: staged writes + deletes into
  the provider, committed via the normal checkpoint path.
- Cache service → `provider.kv` in opaque-bytes mode (picklability
  checks relocate guest-side).
- hostcall registry built from the existing host_objects config;
  typed codec, method allowlist.
- `outputs` replaces `namespace`; ui values flatten guest-side.

Exit: a designated nontainer test subset green on dud-subprocess, plus
new delta tests pinning the *intended* divergences (codec-narrowed
outputs, structured prints, real-bash semantics).

**Containment status through stage 2: zero, and less than today.**
Rung 1 drops sandtrap's walled garden (module gates, network denial,
fs interception) and replaces it with nothing but crash isolation.
Agent code runs as the host user with open egress. Acceptable posture:
own-agent-own-laptop only. Two mitigations available before the VM
rungs:

- **Rung 1.5 (optional)**: wrap the supervisor in Seatbelt (macOS) /
  Landlock (Linux) — fs confined to the scratch dir, network denied
  by default. sandtrap's `[process]` extra contains exactly this
  machinery; it lifts out. Restores accidental-escape containment for
  stage-3 dogfooding at near-zero cost.
- Read stage-3 results carefully: dud-subprocess changes *two*
  variables vs sandtrap mode — fidelity AND open network. Attribute
  quality gains to the right one before concluding the VM rungs are
  justified.

## Stage 3 — studio dogfood + the gate

- Executor toggle in studio (alongside `NONTAINER_STUDIO_ISOLATION`).
- Apps dispatch routed through the executor seam (handlers are just
  `exec_python` with tighter budgets — the symmetry rule).
- Run real sessions; compare agent behavior against sandtrap mode on
  the studio's actual workloads (the analyst + webapp loops).

Exit: **go/no-go on the DESIGN.md kill criteria.** If real-fidelity
execution doesn't measurably improve agent quality and no serving/
untrusted-data need has materialized, stop here — stages 0–2 still
leave nontainer cleaner and a working real-fidelity dev mode.

## Stage 4 — libkrun backend + images (the risky stage)

Spike in this order, each independently de-risking:

1. Boot a bundled-kernel guest on macOS (HVF) with a vsock echo server.
2. OCI → flattened ext4 rootfs (skopeo-style pull, no daemon);
   supervisor injected via a second read-only disk; spec-hash cache
   under `~/.dud`.
3. overlayfs `/workspace` + upperdir diff harvest.
4. Warm residency policy. Note: libkrun has no Firecracker-style
   snapshot/restore (verify current state) — rung 2 leans on fast boot
   (~100–200 ms, bundled kernel) + keeping session VMs warm; warmed
   *snapshots* are a rung-3 feature.

Exit: conformance suite green on libkrun; studio running against a
real VM on a Mac.

## Stage 5 — firecracker backend (Linux)

- jailer integration, no-NIC default, cgroup/memory budgets.
- Warmed post-import snapshots; resume path; measure against the
  latency budget in DESIGN.md (resume 5–20 ms, CoW marginal RAM).
- CI needs a KVM-capable runner (bare metal or nested-virt).

Exit: conformance suite green on all three rungs in CI; measured
numbers published in the README.

## Stage 6 — v2: serving + hardening

- Stateless read-only dispatch pool for published apps.
- hostcall codec hardening review (the boundary's attack surface).
- Incremental materialize (provider commit-diff into existing
  lowerdir) if session-start latency ever warrants it.
- `subtask` host service as the worked delegation example.

## Cross-cutting

- **Conformance suite is the ladder.** One test corpus, three
  backends, run in CI matrix. A rung that can't pass it isn't a rung.
- **Golden transcripts** for runner semantics (echo, harvest, prints
  caps, error rendering) so agent-visible behavior is pinned across
  refactors.
- Protocol versioning from stage 0; no compatibility promises before
  1.0, but the field exists so old guests fail loud, not weird.
