# Roadmap

What's left. Companion to [DESIGN.md](DESIGN.md) (why it's shaped this
way) and [README.md](README.md) (what it is and what it costs).
Updated 2026-07-20.

## Where we are

All three rungs are live and pass the same conformance corpus:

| rung | status |
|---|---|
| `subprocess` | shipped — dev/CI floor, zero isolation |
| `vfkit` (macOS/HVF) | shipped |
| `firecracker` (Linux/KVM) | shipped — including snapshot freeze/thaw parking |

Built and working end to end: OCI image pulls with pip-layered wheels
and pinned debs (pure Python, no daemon), erofs and initramfs rootfs
media, overlay workspace staging at the configured root, the scratch
plane, warm VM pooling with state affinity and demand-driven reclaim,
death recovery, the warm fork template for view execs, and snapshot
parking on firecracker.

## Open

### Firecracker rung

- **Golden snapshots per fingerprint** — boot once, freeze clean, then
  every pool miss thaws a clone instead of cold-booting. The remaining
  half of the parking story.
- **Hardening posture** — jailer, cgroup budgets. The production-grade
  wrapper; not needed for the dev rung.
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
- **hostcall codec hardening**: the boundary's attack surface, before
  anything serves untrusted traffic.

### CI matrix

Conformance runs per-rung locally (`DUD_BACKEND=vfkit`). CI needs:
subprocess everywhere (works today); vfkit on macOS arm64 — open
question whether GitHub's hosted runners allow
Virtualization.framework; may need a self-hosted Mac. Firecracker needs
a KVM-capable Linux runner. **Golden transcripts** — pinning
agent-visible runner semantics (echo, harvest, print caps, error
rendering) across rungs in one corpus — become valuable exactly here.

### Loose ends

- **Boot latency**: ~2.5 s of the 3 s initramfs boot is the guest
  retrying its vsock dial until the VMM's bridge is ready (erofs boots
  in ~1 s). Find or add a readiness signal. Matters less now that pool
  reuse skips boots and recovery is rare, but still paid on pool misses
  and first opens.
- **Studio still defaults to initramfs**; flipping it to `medium="auto"`
  is a one-arg decision, worth bundling with eager-warming work.
- **The dogfood gate was never run.** The behavioral half of DESIGN's
  kill criteria (`dogfood/analyst_agent.py`, needs a model key) — real
  fidelity *and* real isolation versus sandtrap, on the studio's actual
  analyst loop. We proceeded on fidelity evidence instead.

## Deliberately not now

Recorded so they don't get re-litigated.

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
- **Rung 1.5 (Seatbelt/Landlock around the subprocess rung)** — the VM
  rung landing on macOS removed most of its audience. Revisit only if a
  Linux-dev-without-KVM constituency appears.
- **Per-blob content addressing** — kvgit's concern (storage v4
  candidate), not dud's.
- **Windows** — no rung, no plan.
- **Incremental tree push** — `push_tree` wipes and re-extracts on
  restore; fine at current workspace sizes.
