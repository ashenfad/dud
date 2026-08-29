# Changelog

Starts at 0.3.0. Earlier releases predate this file; `git log` is the
record for those.

## Unreleased

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
  not dud's. The old module encoded one consumer's choices — `ui/<name>.plotly.json`, `head(200)` with
  `orient="split"`, an 8 MB cap "for parity with the host renderer" in
  another repo — which made dud know about the layer above it. It is
  now on the same footing as the print renderer: optional, layered
  through the image, resolved from the image rather than the workspace,
  silently absent, and reported by `ping()`.

### Changed behavior

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

- **A reclaimed session releases its owner immediately.** When
  `max_total` pressure reclaims a VM that still has an owner, that
  owner's blocked call now ends at once instead of waiting out its
  deadline.

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
