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
    #                            #      deletes=[])  — paths relative to the root
    # hand d to your versioned store; dud doesn't care what it is
```

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
