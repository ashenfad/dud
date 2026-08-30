# dud 🧨

*A dumb firecracker.* Real, disposable microVMs that never go off:
virtualize the state, not the machine. Tree in, execute against a real
filesystem, diff out. Versioning belongs to the layer above (e.g.
[nontainer](https://github.com/ashenfad/nontainer)'s providers) — dud
is deliberately storage-blind, and the machines are deliberately dumb:
all the smarts live in state, so any VM can vanish at any moment and
nothing of value goes with it.

> **Status: alpha, on PyPI, all three backends live.** The subprocess backend
> (real bash, real Python, zero isolation — own-agent-own-laptop
> posture), the vfkit microVM backend (macOS/HVF), and the firecracker
> microVM backend (Linux/KVM) all pass the same conformance corpus
> over the same wire protocol. OCI-image workspaces with pip-layered
> packages, warm VM pooling with state affinity, a disk-backed scratch
> plane, and snapshot parking on firecracker (a parked VM is inert
> files — zero RAM, ~tens-of-ms resume) all work today. See
> [DESIGN.md](DESIGN.md) for why it's shaped this way and
> [ROADMAP.md](ROADMAP.md) for what's left.

## Install

```bash
pip install dud
```

That's the whole install for the subprocess backend (zero dependencies,
zero isolation). The VM backends each want two more things:

**macOS (vfkit):**

```bash
brew install vfkit          # the VMM
python -m dud.kernels       # the pinned guest kernel (~18 MB, digest-verified)
```

**Linux (firecracker):** a `firecracker` binary on `$PATH` (or point
`$DUD_FIRECRACKER` at one), access to `/dev/kvm`, and the same
`python -m dud.kernels` fetch.

Everything else — OCI image pulls, rootfs builds, scratch volumes —
is pure Python and arrives on first use, cached under `~/.dud`.
Requesting a backend the host can't provide fails loud
(`IsolationUnavailable`) with the missing piece named.

## Quick look

```python
import dud

# backend="subprocess" | "vfkit" | "firecracker" | "vm" (the best VM
# backend for this host — config written against "vm" keeps working
# as new ones land)
with dud.session("vm", image="python:3.12-slim") as s:
    s.shell("mkdir -p data && echo 'a,b\n1,2' > data/in.csv")
    r = s.python("""
import csv
rows = list(csv.reader(open('data/in.csv')))
cache['n'] = len(rows)          # persists across calls (opaque to the host)
emit('status', {'rows': len(rows)})
rows                             # last expression echoes, REPL-style
""")
    print(r.transcript)          # the echo
    print(r.outputs)             # harvested top-level bindings (codec values)

    d = s.diff()                 # Diff(writes={'data/in.csv': b'a,b\n1,2\n'},
    #                            #      deletes=[], modes={})  — paths relative
    #                            #      to the root
    # hand d to your versioned store; dud doesn't care what it is
```

`modes` carries permission bits for the paths that have a non-default
one — `chmod +x` has to survive a checkpoint, or the script an agent
made executable isn't executable next session. Only departures from
`0o644` appear, so it's empty in the common case; `d.mode(path)` reads
it with the default filled in. A mode change on its own counts as a
change, so `chmod +x` on an otherwise untouched file still shows up.

Symlinks and empty directories do not round-trip today.

### Structured output from shell work

`emit` is not Python-only. Shell execs get `dud-emit`, and it lands on
the same `on_emit` callback with the same shape — the host can't tell
which side fired it:

```python
s = dud.session("vm", on_emit=lambda name, value: print(name, value))
s.shell("""
  make test 2>&1 | tail -5
  dud-emit tests '{"failed": 3}'
""")
```

```
dud-emit NAME [VALUE]
```

`VALUE` is JSON if it parses and a plain string otherwise, so both
`dud-emit rows '{"n": 3}'` and `dud-emit status running` do the
obvious thing. The one sharp edge: `dud-emit n 42` emits the *number*
42 — quote it as `'"42"'` for the string. Omitting the value emits
`null`, matching `emit(name)` in Python.

Emits arrive **live, mid-exec**, so a long build can report progress
while it runs rather than at the end, and they survive a timeout —
they're events, not results. Every child bash spawns inherits the
channel, so it works inside `$(...)`, pipelines and background jobs.

One limit worth knowing: an emit has to be *written* before the script
ends. `dud-emit x 1 &` with nothing after it detaches the write from
the script's lifetime, and dud waits only briefly for stragglers — add
`wait` when a backgrounded emit has to be certain. The Python side has
the same rule for the same reason: an `emit()` from a thread that
outlives its exec doesn't arrive either.

This exists because bash is the honest test of whether the emit
contract is language-neutral: no objects, no namespace, no pickle. It
needed no new wire verb and no host-side code — which is the actual
evidence, rather than the ergonomics.

### Where the files live

The guest mounts its workspace at **`/workspace`** (`session(...,
workspace="/path")` to move it), and execs start there — so relative
paths just work, and `/workspace/data/in.csv` is a real absolute path
inside the VM. That matters because it's the *same* path the layer
above teaches: nontainer's local sandbox presents its VFS at the same
root, so agent code that hardcodes an absolute path means the same
file whether it runs in-process or on a real machine.

The root contains exactly the workspace. Staging internals live
outside it, on a separate tmpfs, so a listing never shows dud's
bookkeeping and a write anywhere under the root lands in the diff.
Diff paths are relative to the root.

(The subprocess backend is the exception: a host temp dir can't claim
`/workspace`, so it roots the workspace in its scratch dir —
relative paths behave identically, absolute ones don't. Known gap,
and it only bites if you develop against that backend and deploy on
a VM one.)

### What a print costs to look at

`r.prints` is a structured stream beside the transcript: one entry per
`print`, carrying the text plus the metadata text alone would lose
(`type`, and `shape`/`len` where the object had one). Composing an
observation from those entries is the caller's job — dud knows nothing
about your model or your budget.

The exception is *rendering*, which can only happen guest-side because
it needs the live object — a DataFrame's head/tail can't be
reconstructed from a chopped string:

```python
r = s.python(code, render_budget=200)
# entry text becomes [0, 1, 2, ...86 more] instead of a severed token
```

The number stays yours; unset means plain `str()`. Rendering needs
[reprobate](https://github.com/ashenfad/reprobate) in the image
(`packages=["reprobate"]`) and falls back to plain text without it.
To render with something of your own instead, name it:
`session(render_hook="my_guest_pkg.render:render")` — same
`"module:function"` spelling as `outputs_hook` below. Resolution runs
your hook, then reprobate, then plain `str()`, and
`ping()["renderer"]` says which step is live, including when a named
hook didn't import. Rendered entries are marked `elided`. The
transcript is never rendered — it keeps exactly what real Python
printed.

`caps` are the other knob and are **not** an observation budget: they
bound what one exec can send back so a runaway exec can't flood the
channel, and they sit far above anything you'd show a model. Text is
capped by `stdout`, `entry`, `entries` and `total`; values by `value`
(one binding, `emit`, or hostcall argument) and `outputs` (everything
one exec harvests). The value guards refuse rather than truncate —
half a JSON document is a wrong answer, not a smaller one. `cache` and
`cache_total` bound what one exec stashes, and sit far higher — an
output is an observation, the cache is working storage. `frame`
backstops all of it with a ceiling on the whole body an exec can send,
since names and argument counts ride the wire too; it's derived from
the others by default, so raising one of them raises it too.

### Rich values out

`outputs` carries what the codec can represent: JSON, bytes, file refs.
A live DataFrame or plotly figure can't cross, so out of the box it
just comes back named, with its type, in `outputs_skipped`:

```python
r = s.python("import pandas as pd\ndf = pd.DataFrame({'a': [1, 2]})")
r.outputs          # {}
r.outputs_skipped  # {'pd': 'module', 'df': 'DataFrame'}
```

(`pd` is in there because the harvest is *every* top-level binding,
imports included. dud reports what it couldn't represent rather than
quietly filtering — you decide what's noise.)

A binding that's simply too big lands there too, with its size rather
than just its type — `'str (57.2 MiB exceeds the 8.0 MiB per-value
limit)'`. Everything harvested crosses in one frame the guest
supervisor parses whole, and on a VM rung that supervisor is PID 1, so
the ceiling is what keeps one assignment from taking the machine down.
Raise it with `caps` if you mean it; the usual answer is to write the
value to a workspace file and pick it up from the diff — which is what
an `outputs_hook` is for, and hooks get first refusal before the
ceiling applies.

Nothing is lost — but nothing is delivered either. To get such values
out, serialize them **guest-side** into workspace files, where they
ride home in the diff like any other write. Write an ordinary function
in an ordinary module, put it in the image, and point the session at
it:

```python
# my_guest_pkg/outputs.py — an ordinary module, any name you like
import os


def flatten(bindings: dict, workspace: str) -> set[str]:
    handled = set()
    for name, value in bindings.items():
        if type(value).__module__.startswith("pandas"):
            value.to_csv(os.path.join(workspace, f"{name}.csv"), index=False)
            handled.add(name)   # consumed: don't also send it home
    return handled
```

```python
s = dud.session("vm", image="python:3.12-slim",
                packages=["pandas", "my-guest-pkg"],
                outputs_hook="my_guest_pkg.outputs:flatten")

r = s.python("import pandas as pd\ndf = pd.DataFrame({'a': [1, 2]})")
r.outputs_skipped   # {'pd': 'module'} — df is gone; the hook took it
s.diff().writes     # {'df.csv': b'a\n1\n2\n'}
```

`outputs_hook` is `"module:function"` — the same spelling as a Python
entry point, and colon-separated for the same reason: `a.b.c` can't say
whether `b` is a module or an attribute.

`ping()` tells you where you stand, which matters because a hook that
failed to import produces exactly the same execs as no hook at all:

```python
s.ping()["outputs_hook"]
# "my_guest_pkg.outputs:flatten"              resolved
# "my_guest_pkg.outputs:flatten (not found)"  typo, or not in the image
# None                                        you didn't name one
```

The hook is handed **every** top-level binding and returns the names it
consumed; dud drops those and lets the rest harvest through as usual.
It can also *rewrite* a binding instead of consuming it, which is how a
"collect outputs in a `ui` dict" convention works — read
`bindings["ui"]`, write out the rich entries, put the remainder back,
return nothing.

Note what dud doesn't do here: it names no binding, knows no format,
and picks no default. Which objects to write, in what shape, under what
path is yours, the same way your store is.

### Extension points, in general

Both hooks work the same way: **you name them on the session, the image
provides them.** `"module:function"`, resolved guest-side from the
image and never from workspace files — a module an agent wrote must not
become dud's print or output path, and that holds even if the agent
imported it first. Absent rather than fatal when they don't resolve,
and reported by `ping()`, which answers by resolving exactly as an exec
would: a misspelled *function* name reads `(not found)`, not "live".

They differ in one way, deliberately. `render_hook` has a default and
`outputs_hook` doesn't, because **dud defines the render contract** —
`render(obj, budget)` exists because `render_budget` does — so shipping
a default implementation of it is ordinary. dud does *not* define what
"flattened" means, so any default there would be somebody else's
convention baked into a tool that promises not to have one. That is the
rule for anything added later: name a default when dud defines the
operation, require the caller to name one when you do.

### Pooling and parking

`pooled=True` reuses VMs across sessions from a process-wide warm
pool; `state="<your content hash>"` **parks** a VM — sets it aside
still holding that exact tree — so the next session with the same
content resumes it instead of booting and re-pushing.

How a park is stored differs by backend, but the contract doesn't: on
macOS the VM stays running, on firecracker it's snapshotted to disk —
zero RAM, files that outlive the process that made them.

Host objects cross the boundary as *names*, not references — guest
code gets a proxy whose only power is making allowlisted calls:

```python
s = dud.session(host_objects={"db": my_db}, allow={"db": {"query"}})
s.python("rows = db.query(filter='active')")   # ok
s.python("db.drop_all()")                       # PermissionError, host-side
```

`allow` is **required** for every registered object, and fails closed
like everything else here — registering one without an entry raises
`PolicyError` at construction rather than quietly granting every public
method. Underscore-prefixed names are never callable regardless of what
the allowlist says.

To expose a whole object, resolve it rather than wildcarding it:

```python
allow={"db": dud.public_methods(my_db)}   # every public callable, as a set
allow={"db": set()}                        # registered, nothing callable
```

`public_methods` returns a plain `frozenset`, so the grant stays
something you can print, log and assert on, and it snapshots what
exists *now* — a method added later by a plugin or a monkeypatch isn't
granted retroactively. There is deliberately no `"*"`: a wildcard would
be the easiest thing to type, which is how the old permissive default
became what everyone shipped.

No pickle ever crosses the wire; cache values are opaque bytes to the
host, and everything else rides a tagged json/bytes/file codec.

### When a session dies

Any VM can vanish. That isn't an edge case to defend against — it's the
premise the whole design rests on, and the reason pooling, idle
eviction and capacity pressure need no machinery of their own: they're
all just deliberate versions of the same death.

So there is exactly one thing to catch:

```python
try:
    r = s.python(code)
except dud.SessionLost:
    s.close()                                          # let go of the old one
    s = dud.session("vm", pooled=True, state=commit)   # a new machine
    if not s.resumed:
        s.push_tree(tree)                              # put the tree back
    r = s.python(code)                                 # once
```

**Close the dead session before replacing it.** Rebinding the variable
isn't enough: a pooled VM is held by the pool as well as by you, so
dropping your reference frees nothing. `close()` is what removes it and
tears the machine down — and it matters most for the wedged flavor,
where the VM is still very much alive and still holding its memory.
It's cheap, and never raises.

`SessionLost` covers every way the guest can stop answering — EOF, a
reset, a broken pipe, a pool reclaim tearing a frame mid-call, and a
deadline expiring. Two of those mean different things and it's worth
knowing which you got: *"guest lost during …"* is a machine that went
away, and *"guest did not answer … within Ns"* is one that's still
there and wedged. The first is routine; the second is a bug worth
chasing, usually in guest code.

**A lost session stays lost.** The object refuses further calls rather
than trying the wire again, because a failed request can leave the
channel desynchronized — so a retry on the same session would fail
again with a worse description of what happened. `close()` still works,
and still cleans up.

This is safe to build on because dud never holds the authoritative
tree. Your layer does. Re-acquiring and re-pushing is a complete
recovery by construction, not a best effort — which is also why
`state=` is worth passing: a parked VM already holding that exact
content comes back `resumed=True` and skips the push entirely.

One thing that does *not* roll back: emits. They're events, delivered
live mid-exec, and they're kept even when the exec later fails —
unlike cache writes, which apply only on success. Don't assume
checkpoint atomicity for them.

A protocol version mismatch at connect is deliberately **not** a
`SessionLost` — it stays a `ProtocolError`, because no amount of
re-acquiring will fix a real incompatibility.

## The ladder

The backends aren't peers — they're ordered by how hard a boundary
they put around agent code, and everything above them is identical.
Same guest supervisor, same wire protocol, same conformance corpus;
only the substrate hardens. Hence "the ladder", and **rung** for a
step on it (these docs use "rung" where the ordering is the point and
"backend" where you'd be typing it into `session(backend=...)`).

| rung | backend | platform | isolation |
|---|---|---|---|
| 1 | `subprocess` | any OS | none — dev/CI floor |
| 2 | `vfkit` | macOS (HVF) | real Linux microVM |
| 3 | `firecracker` | Linux/KVM | microVM + snapshot parking (jailer planned) |

The conformance suite in `tests/conformance/` is the contract: a
backend that can't pass it unchanged isn't a rung. Requesting one the
host can't provide raises (`IsolationUnavailable`) — nothing silently
degrades.

(If you know Kubernetes, this is the same idea as RuntimeClass: one
workload contract, swappable isolation underneath.)

## What it costs

Measured on the DS image (numpy/pandas/pyarrow/matplotlib/plotly),
warm caches, erofs root:

| | |
|---|---|
| boot to served channel | ~0.9 s |
| guest RAM at boot | 79 MB (600 MB on the initramfs medium) |
| `exec_python` | ~30 ms |
| read-only view exec | ~117 ms, flat in import weight |
| `diff()` with one change, 210 MB tree | ~1 ms |
| pool hit (warm VM, same image + config) | no boot — reset + push |

The shape behind the numbers: an immutable read-only root demand-pages
from the host page cache instead of living in guest RAM; the workspace
is an overlayfs mount, so a diff is a walk of what changed rather than
a scan of the tree; and view execs fork from a warm import template
instead of paying interpreter startup per request.

## Seeing what it's doing

Most of what the pool does on the failure side is *recoverable* — a
dead parked VM boots fresh, a failed reset discards instead of parking,
an unprewarmable host just runs cold. Recovered silently, all of those
look identical from the outside: boots are mysteriously slow. So dud
logs them, on the stdlib `dud.*` hierarchy:

```python
import logging
logging.basicConfig(level=logging.INFO)      # or attach your own handler
```

| level | what lands there |
|---|---|
| `DEBUG` | pool hits and misses, TTL expiry, blob downloads, scratch promotion misses |
| `INFO` | recoveries a fresh boot papers over, and the first build of a rootfs (the one genuinely slow step) |
| `WARNING` | the two losses somebody pays for: a reclaimed VM that still has an owner, and warmth that couldn't be parked |

dud attaches no handlers and sets no levels — where records go is
yours. `logging.getLogger("dud").setLevel(logging.DEBUG)` turns the
whole tree up; the per-module loggers (`dud.backends.pool`,
`dud.images.builder`, `dud.images.registry`) narrow it.

The guest is the exception: it has no logger to attach to, so guest and
init diagnostics go to the VM console (captured in the session's
rundir) and surface in the `IsolationUnavailable` message on a failed
boot.

## Development

```bash
uv sync --extra dev
uv run pytest
```

VM-backend conformance needs that backend's platform: on macOS,
`DUD_BACKEND=vfkit uv run pytest tests/conformance`; the firecracker
corpus runs on any Linux with `/dev/kvm` — including, on Apple
silicon (M3+), a nested-virt Lima guest (`dev/lima-fc.yaml`,
`dev/fc-test.sh`).

## License

MIT
