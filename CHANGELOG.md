# Changelog

Starts at 0.3.0. Earlier releases predate this file; `git log` is the
record for those.

## Unreleased

The theme is that a session should cost what it does, and that when it
doesn't, you can find out why.

A pool miss no longer boots a machine — it clones one, in ~40 ms
against 1276 ms — which made the firecracker conformance suite go from
~20 minutes to under three. The firecracker rung runs on x86-64, where
most Linux servers are. Every unbounded payload a guest could push
across the wire now has a ceiling, and every ceiling reports itself.
And the two extension points that were discovered by convention are
now named in a signature.

### Breaking

- **Rich value flattening moved out of dud into the image.** The guest
  no longer knows what a plotly figure or a pandas DataFrame is, nor
  that anyone calls their output dict `ui`. It offers every top-level
  binding to a hook you name — `session(outputs_hook="pkg.module:function")`,
  provided by the image — and drops the names it returns (bindings may
  also be rewritten in place). Without a hook, nothing is flattened and
  unrepresentable values land in `outputs_skipped` with their type
  names.

  If you relied on the built-in behavior: put the equivalent function
  in a package, layer it with `packages=[...]`, and name it with
  `outputs_hook=`. `ping()["outputs_hook"]` echoes the spec, marks it
  `(not found)` when it didn't import, and is `None` when you named
  none — so a typo is visible rather than a mystery about where the
  charts went.

  Why: which objects flatten, into what shape, under what path — and
  which binding collects them — is the consuming layer's convention,
  not dud's. The old module encoded one consumer's choices —
  `ui/<name>.plotly.json`, `head(200)` with `orient="split"`, an 8 MB
  cap "for parity with the host renderer" in another repo — which made
  dud know about the layer above it. It is now on the same footing as
  the print renderer: optional, layered
  through the image, resolved from the image rather than the workspace,
  silently absent, and reported by `ping()`.

### Added

- **Golden snapshots: a pool miss clones a machine instead of booting
  one.** Measured on amd64 CI: **32-52 ms to a serving VM, against
  1276 ms cold.**

  A cold boot is a pure function of the boot fingerprint — the same
  kernel running the same `/init` against the same rootfs, to reach a
  state nothing per-session went into. So it is done once per
  fingerprint and cloned afterwards. Sessions are still disposable;
  what is reused is a file, not a machine.

  This is **orthogonal to parking**, and the two solve different
  costs: an affinity park skips `push_tree` because the tree is
  already on that VM; a golden clone skips the boot because the
  machine is already booted. Parking is unchanged.

  Nobody waits for the template. The first miss for a config boots
  normally and gets its machine; the template is built behind it, on
  its own machine, on a background thread — so a session that runs
  once and leaves pays nothing extra. `VmPool(auto_seed=False)` turns
  that off for a consumer who would rather build templates only where
  it says so.

  `VmPool.seed(**kwargs)` builds one ahead of time, which is
  pre-warming without the cost of staying warm: a template holds no
  VM, no RAM and no process, and any number of sessions can start from
  it at once — so unlike `prewarm` there is no warm level to choose
  and nothing to shed.

  Safe because of two firecracker properties, both verified rather
  than assumed (`dev/goldenspike.py`): the memory file maps
  `MAP_PRIVATE`, so clones get copy-on-write and the golden file
  hashes identically after N concurrent clones; and `vsock_override`
  re-points each clone's socket, which is the documented mechanism for
  restoring one snapshot into several VMs. Randomness does not
  collide across clones either — `os.urandom`, `uuid4` and `random`
  all measured distinct, since virtio-rng reseeds on resume.

  **Releases no longer freeze.** Parking a VM on this rung meant
  snapshotting it — ~3 s for a 1 GiB guest — to save what is now a
  40 ms clone, which made pooling *slower* than not pooling. A plain
  release now tears the machine down; the next miss clones a fresh
  one, which comes up just as warm and cleaner. Freeze now has exactly
  one use left: building the template, once per config.

  An **affinity park** — a release carrying `state=` — is the one
  thing a clone cannot reproduce, because it holds a workspace. Those
  are kept, and kept *running* rather than frozen — freezing them
  would put three seconds on every release that took the path, to save
  RAM on a VM you are parking precisely because you expect to come
  straight back to it. Off by default; see `max_affinity` below.

  The template is keyed by the guest-code identity as well as the boot
  fingerprint, and carries a manifest naming the rootfs and kernel it
  was booted from, checked before every restore — so upgrading dud
  cannot resume a guest built from the previous release. Configs using
  `scratch` do not use templates at all: a snapshot records the seed
  VM's per-boot volume, which does not outlive it. A background seed is
  a whole VM and counts against `max_total` like any other, so a pool
  at its cap builds no template until there is room.

  Every failure falls back to booting. A golden snapshot is a cache:
  not having one, or having one that will not restore, costs speed and
  never a session.

- **The firecracker rung runs on x86-64 Linux.** It was described as a
  disposable Linux/KVM microVM but pinned only arm64 assets, so on the
  architecture most Linux servers are it raised
  `IsolationUnavailable: no guest kernel for amd64`. Both the kernel
  and the four Debian packages are now pinned for amd64 as well, at
  the same versions arm64 uses — one kernel (6.18.35) across both, so
  one conformance corpus still means one kernel.

  `python -m dud.kernels` fetches the right one for the host; nothing
  else changes, and arm64 artifacts are byte-identical.

  Making it actually work took one non-obvious fix. PID 1 asked for a
  power-off, which aarch64 turns into a PSCI `SYSTEM_OFF` the VMM
  handles — but firecracker on x86-64 implements no ACPI, so nothing
  claims `pm_power_off` and Linux's power-off path ends in a halted
  CPU. The guest went idle rather than away, and nothing failed
  loudly: the host's `shutdown` succeeded and the cost arrived as
  wall-clock, waiting out the VMM-exit timeout on every teardown. It
  also silently disabled scratch promotion, since a volume is only
  promotable after a clean exit. The guest now asks for a *reboot* on
  x86-64, which `reboot=k` routes to the i8042 reset line firecracker
  traps.

  Firecracker conformance now also runs in CI on every pull request,
  on a hosted `ubuntu-latest` runner with `/dev/kvm`. Previously it
  ran only by hand against a nested dev VM that degrades with use —
  so a red run had to be diagnosed before it could be believed.

- **`dud-emit`: the emit channel, reachable from bash.** Shell execs
  can now report structured events, not just transcript text:

  ```bash
  make test 2>&1 | tail -5
  dud-emit tests '{"failed": 3}'
  ```

  They land on the same `on_emit` callback, in the same shape, as an
  `emit()` from Python — there is no second verb and no host-side
  branch, so the host cannot tell which side fired one. `VALUE` is
  JSON if it parses and a plain string otherwise (`dud-emit n 42`
  emits the number; `'"42"'` is the string); omitting it emits `null`.

  Emits arrive live, mid-exec, so a long build reports progress as it
  goes, and they survive a timeout — they are events, not results.
  Every child bash spawns inherits the channel, so `$(...)`,
  pipelines and background jobs all work. An emit still has to be
  *written* before the script ends: `dud-emit x 1 &` with nothing
  after it detaches the write from the script's lifetime, and dud
  waits only briefly for stragglers, so add `wait` when it has to be
  certain. Python has the same rule — an `emit()` from a thread that
  outlives its exec does not arrive either.

  Why it exists at all: DESIGN names bash as the forcing function for
  the emit contract — no objects, no namespace, no pickle, so a
  contract that is ergonomic from there cannot have smuggled in a
  language assumption. That claim had been asserted in the doc and
  absent from the code since the contract was written.

  Not included, deliberately: `cache` from bash. Emit is
  fire-and-forget over a pipe; cache is request/response and would
  need a channel back into a supervisor that is mid-`select`.

- **`session(render_hook="pkg.module:function")`** names your own print
  renderer, resolved from the image ahead of the reprobate default.
  Previously the only way to override rendering was to ship a package
  literally named `reprobate` — name-squatting as an extension
  mechanism. `ping()["renderer"]` reports which step of the chain (your
  hook, reprobate, plain `str`) is actually live, including when a
  named hook failed to resolve.

- **`session()` names its extension points.** `host_objects`, `allow`,
  `cache`, `on_emit`, `outputs_hook`, `render_hook`, `image`,
  `packages` and `memory_mib` are explicit keyword parameters instead
  of anonymous `**kwargs`, so `help(dud.session)`, autocomplete and a
  type checker all show what the one blessed entry point actually
  takes. The rest of a backend's constructor still passes through. The
  three rungs' identical pooled/state handling collapsed to one copy
  while there.

- **`max_affinity` on `VmPool`** (default 0 — off): how many tagged VMs
  to keep hot *per boot fingerprint* for a same-content resume. An
  affinity park now buys exactly one thing — a skipped `push_tree` —
  since a plain miss clones in ~40 ms instead of booting, so whether it
  is worth a 1-2 GiB guest is entirely a question of what a push costs.
  Measured (`dev/pushbench.py`): ~40 us per file, linearly, out to 20k
  files. On the dozens-of-files workspaces this actually serves that is
  **3-9 ms**, against a ~45 ms acquire — so it is off unless asked for.
  It pays at scale (418 ms at 10k files), which is why it is a knob.

  Setting it to 0 would otherwise make `park_state` silently do
  nothing, so a tagged release with affinity off now logs a warning
  once per pool naming the mismatch, rather than leaving a caller to
  infer it from `resumed=False` forever. `$DUD_VM_MAX_AFFINITY` turns
  it on for the shared pool, since `dud.session(pooled=True, state=...)`
  builds its pool there and would otherwise be asking for something it
  had no way to enable. Deliberately not inferred from a caller passing
  `state=`: a consumer who tags every session is exactly the one who
  would end up holding a VM per fingerprint without choosing to.

- **`ping()` reports whether the image shipped precompiled bytecode.**
  Baking is skipped when the host interpreter's minor version differs
  from the image's, which is now the common case — the default image is
  `python:3.12-slim` and hosts are increasingly 3.13/3.14 — and it was
  invisible: nothing raised, behavior was identical, and CI could not
  catch it because CI pins the matching version so that baking happens.
  `ping()["bytecode"]` is `"baked"` or `"skipped: host python X != guest
  Y"`, read from the build metadata so a cache hit reports as
  accurately as a fresh build. Both bakes now log the skip too.

  Note this is a performance property and deliberately not part of the
  rootfs spec hash, so two hosts on different interpreters share one
  cache entry and whichever built it first decides what is in it. The
  field is how to tell which you got — and it reports `"unknown"`
  rather than guessing when the artifact and its metadata disagree,
  which two such builders publishing concurrently can produce.

- Hook resolution refuses a module that lives in the workspace even
  when the agent imported it first, so a file agent code wrote cannot
  become dud's print or output path. `ping()` resolves the same way an
  exec does, so it no longer reports a hook as live when only its
  module (and not its function) exists.

### Changed behavior

- **Guests boot faster.** Two changes, measured back to back on
  firecracker under nested virt:

  The rootfs now ships **baked bytecode**. It never had any:
  `python:*-slim` deletes every `.pyc` at image-build time, and dud's
  bytecompile step only ever covered layered wheels — so a guest
  compiled ~1100 stdlib modules on the way up, and on a read-only
  erofs root could never cache the result, paying it again on every
  exec. Verified against a real cached rootfs: 1116 stdlib `.py`, zero
  `.pyc`. Baking them cut boot from **5.61 s to 4.85 s** and removes a
  recurring per-exec cost on erofs entirely. Bytecode is
  minor-version-scoped, so this bakes only when the host interpreter
  matches the guest's and otherwise skips silently, exactly as the
  wheels path already did.

  The kernel cmdline also suppresses probes for devices a microVM does
  not have (`i8042.*`, `nomodule`, `swiotlb=noforce`,
  `cryptomgr.notests`), from firecracker's own boot-time test. No
  measurable gain on aarch64 — those are x86 devices — but they cost
  nothing and should help on x86-64.

  The serial console stays on deliberately, though firecracker's test
  drops it: it is how a boot failure gets explained, and it is what
  identified an amd64 poweroff bug that no host-side error described.

- **A wedged guest now fails instead of hanging.** Every host→guest
  request carries a wall-clock deadline, so a guest that stops
  answering raises `SessionLost` rather than blocking its caller
  forever. Death always recovered on its own — the channel closes and
  the owner re-acquires — but a hang had nothing to recover *from*.

  The budget is per verb rather than one number, because a `ping` and a
  `push_tree` of a 200 MB tree are not the same wait: execs derive
  theirs from the `timeout` you already pass (plus the guest's
  kill-and-report tail), `push_tree` from the payload size, and the
  rest are fixed ceilings. All of them bound a *stuck* guest rather
  than expressing a service level — the operations themselves are
  milliseconds — so nothing healthy should come near one.

  The one workload that could newly fail is a `push_tree` slower than
  60 s plus a second per 10 MB. If you push trees that large over a
  slow link, that is the number to know.

- **`SessionLost` now covers the whole loss surface.** It already meant
  EOF, reset and broken pipe; it now also covers a deadline expiring,
  and the framing errors (`ProtocolError`, JSON/UTF-8 decode failures)
  that a pool reclaim can produce by tearing a frame under an owner
  mid-call. Consumers still catch exactly one thing, which is what the
  recovery contract always promised.

  A protocol version mismatch at connect is deliberately *not* one of
  these: it stays a `ProtocolError`, because it is a real
  incompatibility and no amount of re-acquiring will fix it.

- **A lost session stays lost.** After `SessionLost`, further calls on
  the same object raise immediately instead of reaching the wire, with
  a message naming what actually died and what was refused. `close()`
  still works — the latch guards the wire, not the object — and a
  pooled VM clears it on reuse.

  This was already the documented contract ("the session object is
  unusable afterward"); nothing enforced it, so a caller who caught
  `SessionLost` and retried on the same session — the natural reading
  of "retry once" — got undefined behavior. A failed request can leave
  the channel desynchronized: a deadline can expire mid-frame, and a
  guest that was merely slow leaves its late response in the channel,
  so the next request reads a foreign id. Either way the *second*
  failure described what happened worse than the first did.

  README now documents the recovery contract end to end — what raises,
  what the two flavors of loss mean, and why re-acquiring plus
  re-pushing is a complete recovery rather than a best effort.

- **A reclaimed session releases its owner immediately.** When
  `max_total` pressure reclaims a VM that still has an owner, that
  owner's blocked call now ends at once instead of waiting out its
  deadline.

- **The pool no longer holds VMs nobody can reach.** Two ways it did:

  A lost session that its owner rebound rather than closed stayed in
  the pool's bound set forever. Dropping your reference frees nothing,
  because the pool holds one too — so a wedged VM kept its memory for
  the life of the process. `acquire` now reaps bound sessions the wire
  has already failed on. This is not the `max_total` reclaim, which
  interrupts a *live* session deliberately: a lost one can never be
  used again, so collecting it takes nothing from anybody.

  And `VmPool.close()` only ever tore down *idle* VMs, which meant the
  `atexit` path left every checked-out session untouched. It now takes
  both. The leak had an alibi — the process-exit cascade powers guests
  off regardless, so no VM outlived the run — but the bookkeeping and
  the rundirs did, until the next boot's stale sweep.

  Closing a dead session before replacing it is still the right thing
  to do, and README's recovery example now shows it.

- **`shell()` transcripts are capped**, at the same 1 MiB the Python
  runner has always applied to its own transcript. Past the cap the
  head is kept and the transcript ends with a line saying how much was
  dropped. The script still runs to completion — the bound stops
  *storing* output, it does not close the pipe and stall the writer.

  Why it needed one: an uncapped transcript let any chatty script size
  the supervisor's memory, and on a VM rung the supervisor is PID 1
  with the machine's whole RAM. One second of `yes` produced 200 MB.
  Nothing about it required malice — `cat` a large log, run a verbose
  build. If you were relying on transcripts above 1 MiB, write the
  output to a workspace file and read it from the diff.

- **Values crossing from the guest are now bounded.** A harvested
  binding, an `emit` value and a hostcall argument each cap at 8 MiB
  on the wire, and one exec's harvest caps at 32 MiB in total. Both
  are `caps` keys — `value` and `outputs` — so a caller who means it
  can raise them per exec, the same way the text guards work.

  They refuse rather than truncate, because half a JSON document is a
  wrong answer and not a smaller one. What happens next differs by how
  the value was offered: a harvested binding is *implicit*, so it
  lands in `outputs_skipped` with its size (`'str (57.2 MiB exceeds
  the 8.0 MiB per-value limit)'`) and the exec succeeds; an `emit` or
  a hostcall argument is an *explicit call the agent wrote*, so it
  raises there — a dropped event the host can't distinguish from one
  that never fired is the worst shape for an event.

  An `outputs_hook` still gets first refusal, before any ceiling
  applies. That ordering is the point: a 200 MiB DataFrame is exactly
  what a hook exists to turn into a workspace file, and capping first
  would refuse it before the thing that knows how to keep it ever saw
  it.

  Why they were needed: all three cross in the JSON body of a frame
  the guest supervisor parses whole, and on a VM rung that supervisor
  is PID 1 with the machine's RAM. One 60 MB assignment crossed in
  0.5 s with nothing in the way.

- **Cache writes are bounded too, generously.** 64 MiB for one write
  and 128 MiB across one exec, as `caps["cache"]` and
  `caps["cache_total"]`. Eight times the ceiling on a harvested value,
  because the two are different jobs: `outputs` carries an
  observation, the cache is working storage, and stashing data between
  execs is what it is *for*. Ordinary use should never meet these; a
  30 MB DataFrame stashes exactly as before.

  Over the ceiling the **exec fails** rather than the write being
  dropped — `cache[k] = v` is something the agent asked for by name,
  and a stash that quietly did not happen surfaces next session as an
  unexplained miss. The transcript and prints survive the failure, so
  an exec whose only fault was the size of its last statement does not
  also lose the evidence of what it did.

  Sizes are measured at flush rather than at assignment, because
  in-place mutation is a supported way to write
  (`cache["x"].append(...)` is captured), so the value at assignment
  is not the value that ships.

  These bound one *transit*. How large the cache may grow across a
  session remains the consuming layer's question (see ROADMAP,
  "cache-as-service semantics").

  Because a frame is more than the values in it, the guest channel
  also carries a ceiling on the whole JSON body it will send —
  `caps["frame"]`, derived by default from the other caps so raising
  one of those can never leave this as the thing that refuses an exec.
  Per-value limits are what give a good error and a precise
  `outputs_skipped` entry; this is what makes the bound a guarantee.
  It covers the three ways past a value-shaped check:

  - a **binding name** (`globals()['k' * 40_000_000] = 1` charged one
    byte to the total and put 40 MB in the frame — names now count,
    and an oversized one is reported under a truncated key, since
    filing it under itself would put it on the wire anyway);
  - an **emit name**, bounded like its value;
  - **many individually-legal hostcall arguments** — twenty of 7 MiB
    each pass an 8 MiB per-value check and assemble into 140 MB.

  `dud.ValueTooLarge` and `dud.FrameTooLarge` are the new exceptions.
  The first subclasses `NotRepresentable` so existing handling keeps
  working; the second is raised before anything is written, so the
  channel stays usable.

### Fixed

- **Pooled reuse was slower than booting a fresh VM.** `reset_guest`
  took a flat 2.01 s, against a 0.94 s cold boot on vfkit — so the
  pool was a pessimization on that rung, and on firecracker it gave
  back most of what parking saves.

  The reset kills every non-PID-1 process and then waits for the
  machine to be idle again, bounded at 2 s. It counted **kernel
  threads**, which are unkillable by design and which a guest has
  dozens of — so "only PID 1 remains" could never become true and the
  sweep always ran its whole budget. It now skips them the way `ps`
  and `top` do, by an empty `cmdline`.

  Measured after: **2.01 s → 0.02 s.** Every pooled release pays this,
  so it is a straight win for any consumer using `pooled=True`, not
  only for tests.

- **A background process no longer holds a `shell()` call open.**
  `shell("nohup server &")` returned only when the *server* exited,
  because the call waited for end-of-pipe and the server had inherited
  stdout — so an instant, successful script burned its entire timeout
  and came back `timed_out=True`. The call now ends when the script
  ends, and reports the script's own exit code.

- **A shell timeout can no longer wedge the guest.** The drain after
  the kill was an unbounded read, so one process that escaped the
  process-group kill (anything that calls `setsid`) blocked the
  single-threaded supervisor for as long as that process lived. On a VM
  rung that cost the whole machine. The drain is now bounded by a
  deadline as well as by EOF, matching what the Python runner's spill
  already did.

- **A shell timeout no longer surfaces as `PermissionError`.** On macOS
  the process-group kill raises EPERM — not ESRCH — when the group's
  only remaining member is an unreaped corpse, which is the ordinary
  state at a timeout whose script had already exited. The error escaped
  as a `RemoteError` from `exec_shell` instead of a normal
  `timed_out=True` result.

### Upgrading

Rootfs artifacts rebuild once on first use (`PIPELINE_VERSION` 3 → 5);
image pulls are cached, so this is a rebuild rather than a re-download.
Golden snapshots from a previous dud are ignored rather than reused.

## 0.3.0 - 2026-08-20

### Breaking

- **`allow` is now required for every registered host object.**
  `session(host_objects={"db": db})` without a matching entry raises
  `PolicyError` at construction instead of granting guest code every
  public method. The allowlist is the only fine-grained gate between
  agent code and a live host object, and it was the one place dud
  failed open.

  ```python
  allow={"db": {"query"}}                  # name the methods
  allow={"db": dud.public_methods(db)}     # all of them, as a resolved set
  allow={"db": set()}                      # registered, none callable
  ```

  `public_methods` is deliberately not a `"*"` wildcard: it resolves to
  a plain frozenset, so the grant stays inspectable and snapshots what
  exists now rather than whatever a plugin adds later.

  Allow values are also validated. A bare string (`allow={"db":
  "query"}` — braces dropped) used to match by *substring*, so a
  one-character typo widened the grant; that now raises.

### Changed behavior

- **The guest boots with its image's environment.** It was captured at
  build time, written to `meta.json`, and never applied — so the guest
  ran with no `PATH` at all. A populated `PATH` changes what bare
  commands resolve to, and `LANG=C.UTF-8` shifts encoding defaults.

  Mostly this repairs things that were broken: agent code can now
  `subprocess` python, pip and anything `packages=[...]` layers into
  `/usr/local/bin`, and a Python started by bash knows its own
  `sys.executable` (so `python -m venv` works). Image ENV overrides the
  kernel's `HOME=/` and `TERM=linux`; `WORKDIR` is deliberately not
  applied, since execs start at the workspace root by contract.

- **Print guards are far looser.** The old 20 KB transcript / 2 KB
  entry / 200 entry defaults were sized like an observation budget, so
  a 10,000-character print came back truncated twice over before the
  host ever saw it. They are now resource guards — 1 MiB / 16 KiB /
  2000, plus a 2 MiB total across entries — and deciding what a model
  should see is the caller's job.

### Fixed

- **Symlinks are no longer destroyed at checkpoint.** On a VM rung
  `diff(rebase=True)` deleted them from the agent's live workspace; on
  the subprocess rung a live one produced a spurious delete for a file
  that still existed, and a dangling one raised. Symlinks still don't
  round-trip to a consumer — but not carrying something and losing it
  are different.

- **`chmod +x` survives a checkpoint.** Permission bits now ride the
  diff as `Diff.modes` (only departures from `0o644`; `Diff.mode(path)`
  reads it with the default filled in). A mode change on its own counts
  as a change, so `chmod +x` on an otherwise untouched file is
  reported. setuid and friends are masked at the boundary.

- **A failed exec keeps its transcript.** A timeout or a crash returned
  `transcript: ""`, discarding everything the code had printed at
  exactly the moment it was worth most. Now the output survives — 
  including native output like a segfault notice, which never passed
  through Python's stderr at all.

- **Concurrent writers can't corrupt the artifact cache.** Registry
  blobs, pinned debs and rootfs images shared one staging path, so two
  writers of the same artifact could publish a torn file — and in the
  blob cache, publish it under a content-addressed name that nothing
  re-verifies.

### Added

- **`render_budget`** on `session.python()`: renders print entries with
  structural elision (`[0, 1, 2, ...86 more]`) instead of a mid-token
  cut. Needs `reprobate` in the image; falls back to plain text, and
  `ping()["renderer"]` reports which is live.

- **Logging** on the stdlib `dud.*` hierarchy. The pool recovers from
  almost everything — a dead parked VM, a guest that won't reset, a
  host that can't prewarm — and recovered silently those are
  indistinguishable from "boots are mysteriously slow". `WARNING` is
  reserved for the two losses somebody pays for. dud attaches no
  handlers and sets no levels.

- **`dud.__version__`** now reads installed package metadata. It was a
  literal, and had reported `0.0.1` since the initial commit.
