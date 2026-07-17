# dud 🧨

*A dumb firecracker.* Real, disposable machines for versioned agent
workspaces: tree in, execute against a real filesystem, diff out.
Versioning belongs to the layer above (e.g.
[nontainer](https://github.com/ashenfad/nontainer)'s providers) — dud
is deliberately storage-blind.

> **Status: pre-alpha, rung 1 only.** The subprocess backend works
> (real bash, real Python, zero isolation — own-agent-own-laptop
> posture). The microVM rungs (libkrun on macOS/Linux, Firecracker on
> Linux) are designed but not built. See [DESIGN.md](DESIGN.md) for
> the full rationale and [PLAN.md](PLAN.md) for staging.

## Quick look

```python
from dud import Session

with Session() as s:
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

Host objects cross the boundary as *names*, not references — guest
code gets a proxy whose only power is making allowlisted calls:

```python
s = Session(host_objects={"db": my_db}, allow={"db": {"query"}})
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
| `subprocess` (today) | any OS | none — dev/CI floor |
| `libkrun` (planned) | macOS + Linux | real Linux microVM |
| `firecracker` (planned) | Linux/KVM | microVM + jailer, snapshots |

The conformance suite in `tests/conformance/` is the contract: a
future rung that can't pass it unchanged isn't a rung.

## Development

```bash
uv sync --extra dev
uv run pytest
```

## License

MIT
