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
bound what one exec can send back so a runaway print loop can't flood
the channel, and they sit far above anything you'd show a model.

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
image and never from workspace files (a `reprobate.py` an agent wrote
must not become dud's print path), absent rather than fatal when they
don't import, and reported by `ping()`.

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
