# Changelog

Starts at 0.3.0. Earlier releases predate this file; `git log` is the
record for those.

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
