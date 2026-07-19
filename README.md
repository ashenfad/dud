# dud 🧨

*A dumb firecracker.* Real, disposable microVMs that never go off:
virtualize the state, not the machine. Tree in, execute against a real
filesystem, diff out. Versioning belongs to the layer above (e.g.
[nontainer](https://github.com/ashenfad/nontainer)'s providers) — dud
is deliberately storage-blind, and the machines are deliberately dumb:
all the smarts live in state, so any VM can vanish at any moment and
nothing of value goes with it.

> **Status: pre-alpha, all three rungs live.** The subprocess backend
> (real bash, real Python, zero isolation — own-agent-own-laptop
> posture), the vfkit microVM backend (macOS/HVF), and the firecracker
> microVM backend (Linux/KVM) all pass the same conformance corpus
> over the same wire protocol. OCI-image workspaces with pip-layered
> packages, warm VM pooling with state affinity, a disk-backed scratch
> plane, and snapshot parking on firecracker (a parked VM is inert
> files — zero RAM, ~tens-of-ms resume) all work today. See
> [DESIGN.md](DESIGN.md) for the rationale, [PLAN.md](PLAN.md) for the
> original staging, and [ROADMAP.md](ROADMAP.md) for what's next.

## Quick look

```python
import dud

# backend="subprocess" | "vfkit" | "firecracker" | "vm" (best VM rung
# for this host — config written against "vm" survives new rungs)
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

    d = s.diff()                 # {'data/in.csv': b'a,b\n1,2\n'}, deletes=[]
    # hand d to your versioned store; dud doesn't care what it is
```

`pooled=True` reuses VMs across sessions from a process-wide warm
pool; `state="<your content hash>"` parks and resumes a workspace
without re-pushing it. On macOS a parked VM stays hot; on firecracker
it's frozen to disk — same contract, better economics.

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

Same guest supervisor, same wire protocol on every rung — only the
substrate hardens:

| rung | platform | isolation |
|---|---|---|
| `subprocess` | any OS | none — dev/CI floor |
| `vfkit` | macOS (HVF) | real Linux microVM |
| `firecracker` | Linux/KVM | microVM + snapshot parking (jailer planned) |

The conformance suite in `tests/conformance/` is the contract: a rung
that can't pass it unchanged isn't a rung. Requesting a rung the host
can't provide raises (`IsolationUnavailable`) — nothing silently
degrades.

## Development

```bash
uv sync --extra dev
uv run pytest
```

VM-rung conformance needs the rung's platform: on macOS,
`DUD_BACKEND=vfkit uv run pytest tests/conformance`; the firecracker
corpus runs on any Linux with `/dev/kvm` — including, on Apple
silicon (M3+), a nested-virt Lima guest (`dev/lima-fc.yaml`,
`dev/fc-test.sh`).

## License

MIT
