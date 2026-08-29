"""The Python runner: run_python without sandtrap.

One process per exec (spawned by the supervisor; killed on timeout).
Plumbing only, no policy — the boundary is elsewhere (DESIGN.md "The
Python runner"). Jobs:

- build globals: decoded ``inputs`` as bindings, ``cache`` as a
  read-through/write-back view over the channel, each host-object
  registration as a dumb ``HostProxy`` (the only thing it can produce
  is hostcalls; the host validates every one)
- plain ``exec``, with last-expression echo via a ten-line ast split
- ``print`` shadowed for structured capture (text + type metadata,
  per-entry caps) alongside the transcript
- harvest top-level bindings post-exec into the Value codec
- cache writes buffer locally and ride the result — applied host-side
  only on success, atomic with the call's checkpoint

Invoked as: python -m dud.guest.runner <socket-fd>
The exec request arrives as the first (and only) ``run`` request on
that socket; cache/hostcall/emit flow back as reverse requests.
"""

from __future__ import annotations

import ast
import io
import os
import pickle
import sys
import traceback
from collections.abc import MutableMapping
from contextlib import redirect_stderr, redirect_stdout
from typing import Any

from ..proto import Channel, RemoteError
from ..values import decode_map, encode_map, encode_value

_RUNNER_FILE = "<session>"

# Out-of-band slot for binary payloads travelling with a result dict.
# Popped by whoever hands the dict to the wire, so it never reaches a
# body. Shared with the supervisor, which uses it in the other
# direction — see dud.guest.supervisor.
_BINS = "_bins"

# Resource guards, deliberately not an observation budget: what a model
# should see is the host's call, and it has the budget, the model and
# the turn to decide with. These only stop a runaway print loop from
# flooding the channel or ballooning a PID-1 supervisor, so they sit far
# above any plausible observation — the old 20 KB / 2 KB / 200 defaults
# were quietly making that decision on the host's behalf.
_CAP_STDOUT = 1 << 20   # 1 MiB of transcript
_CAP_ENTRY = 1 << 14    # 16 KiB per print
_CAP_ENTRIES = 2_000    # entries in the structured stream
_CAP_TOTAL = 1 << 21    # 2 MiB across entries — bounds entry * count

# Smallest render budget worth giving one print argument; below this a
# value renders to punctuation. Always clamped to the budget that is
# actually left, so it bounds legibility rather than the total.
_MIN_ARG_BUDGET = 16


class _TeeBuffer(io.StringIO):
    """The transcript buffer, mirrored out of the process as it fills.

    Everything written here is also pushed to the inherited stdout —
    which the supervisor holds the read end of — and flushed, because
    block buffering on a pipe would leave it sitting in memory that a
    kill destroys.

    On a normal exec the mirror is redundant: the run response carries
    this buffer's contents, capped and structured, and the supervisor
    throws its copy away. It exists for the two exits that never send a
    response — a timeout and a crash — where the buffer dies with the
    process and the mirror is the only surviving evidence of how far
    the code got. Those are the failure modes a real machine invites
    (C extensions segfault, allocations OOM), and the moment output is
    worth the most.
    """

    def __init__(self, mirror):
        super().__init__()
        self._mirror = mirror

    def write(self, s: str) -> int:
        n = super().write(s)
        try:
            self._mirror.write(s)
            self._mirror.flush()
        except (OSError, ValueError, AttributeError):
            # No mirror (closed, or none inherited): the in-process
            # transcript still works, we just lose the failure copy.
            pass
        return n


class CacheView(MutableMapping):
    """dict-like over the host cache: lazy read-through, local
    write-back. Pickling happens only here, guest-side; the host
    stores opaque bytes.

    ``readonly`` (a GET app handler's cache view — structural REST):
    writes and deletes raise ``PermissionError`` instead of buffering,
    matching the host-side read-only cache. Reads are unaffected."""

    def __init__(self, channel: Channel, readonly: bool = False):
        self._ch = channel
        self._readonly = readonly
        self._local: dict[str, Any] = {}
        self._fetched: dict[str, bytes] = {}  # pickle bytes as read
        self._deleted: set[str] = set()
        self._known_missing: set[str] = set()

    def __getitem__(self, key: str) -> Any:
        if key in self._local:
            return self._local[key]
        if key in self._deleted or key in self._known_missing:
            raise KeyError(key)
        body, bins = self._ch.request("cache.get", {"key": key})
        if not body.get("hit"):
            self._known_missing.add(key)
            raise KeyError(key)
        raw = bins[0] if bins else b""
        value = pickle.loads(raw)
        self._local[key] = value
        self._fetched[key] = raw
        return value

    def __setitem__(self, key: str, value: Any) -> None:
        if self._readonly:
            raise PermissionError("cache is read-only in GET handlers")
        self._local[key] = value
        self._deleted.discard(key)
        self._known_missing.discard(key)

    def __delitem__(self, key: str) -> None:
        if self._readonly:
            raise PermissionError("cache is read-only in GET handlers")
        found = key in self._local
        if not found:
            try:
                self[key]
                found = True
            except KeyError:
                found = False
        if not found:
            raise KeyError(key)
        self._local.pop(key, None)
        self._deleted.add(key)

    def __iter__(self):
        body, _ = self._ch.request("cache.keys", {})
        keys = set(body.get("keys", [])) | set(self._local)
        return iter(k for k in sorted(keys) if k not in self._deleted)

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def flush(self) -> tuple[dict[str, bytes], list[str]]:
        """(writes as raw pickles, deletes) for the result payload.

        Raw bytes, not base64: these ride binary frames beside the
        response so an unbounded pickle never becomes a JSON string.

        Keys that were only read ship back only if their re-pickled
        bytes differ from what was fetched — that keeps in-place
        mutation capture (``cache["x"].append(...)``) without turning
        every read into a spurious write upstream.
        """
        writes = {}
        for k, v in self._local.items():
            raw = pickle.dumps(v, protocol=pickle.HIGHEST_PROTOCOL)
            if self._fetched.get(k) != raw:
                writes[k] = raw
        return writes, sorted(self._deleted)


class HostProxy:
    """A name the guest can talk at, not an object it can reach into."""

    def __init__(self, name: str, channel: Channel):
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_ch", channel)

    def __getattr__(self, method: str):
        if method.startswith("_"):
            raise AttributeError(method)
        name, ch = self._name, self._ch

        def call(*args, **kwargs):
            enc_args, skipped_a = encode_map({str(i): a for i, a in enumerate(args)})
            enc_kwargs, skipped_k = encode_map(kwargs)
            if skipped_a or skipped_k:
                bad = list(skipped_a.values()) + list(skipped_k.values())
                raise TypeError(
                    f"{name}.{method}: arguments of type {bad} can't cross "
                    "the boundary (json/bytes only)"
                )
            try:
                body, _ = ch.request(
                    "hostcall",
                    {"obj": name, "method": method,
                     "args": [enc_args[str(i)] for i in range(len(args))],
                     "kwargs": enc_kwargs},
                )
            except RemoteError as e:
                raise RuntimeError(f"{name}.{method}: {e.message}") from None
            from ..values import decode_value
            return decode_value(body["result"]) if "result" in body else None

        call.__name__ = method
        return call

    def __repr__(self) -> str:
        return f"<host object {self._name!r}>"


def _workspace_root(workspace: str | None) -> str | None:
    """The one directory a hook must not be loaded from: the agent's.

    Deliberately NOT cwd, though cwd is still stripped from
    ``sys.path``. The two are the same directory in the runner and very
    different elsewhere: on the subprocess rung the supervisor inherits
    the host's cwd, so treating that as agent-writable refused every
    module in the project's own virtualenv — including reprobate, which
    made ``ping`` report "plain" while execs were still rendering.
    """
    cand = workspace or os.environ.get("DUD_WORKSPACE")
    if not cand:
        return None  # unknown: fall back to path filtering alone
    try:
        real = os.path.realpath(cand)
    except OSError:
        return None
    return real if real != os.sep else None


def _is_agent_authored(origin: str, root: str) -> bool:
    try:
        real = os.path.realpath(origin)
    except OSError:
        return True  # can't tell where it came from: refuse it
    return real == root or real.startswith(root + os.sep)


def _from_image(module: str, attr: str, workspace: str | None = None):
    """Resolve ``module.attr`` from the IMAGE, never from workspace files.

    dud has two extension points — the print renderer and the outputs
    hook — and both are ordinary imports, which is exactly the problem.
    cwd is the workspace and ``python -m`` puts cwd on sys.path, so a
    file beside the agent's own code can shadow the real package, and
    dud would be running agent-authored code as its own print or output
    path.

    Two defenses, because one is not enough. Stripping the workspace
    from ``sys.path`` stops the shadow being *found* — but only for an
    import that actually searches the path. Agent code runs BEFORE the
    harvest, so it can ``import`` the shadow itself first, and
    ``import_module`` then returns it straight out of ``sys.modules``
    without consulting ``sys.path`` at all. (Measured, not theorized:
    an agent that wrote its own hook module and imported it had its
    function called in place of the configured one.) So the resolved
    module's ``__file__`` is checked too, and one living under the
    workspace is refused however it got there.

    A namespace package has no ``__file__`` and no code of its own, so
    there is nothing to hijack and nothing to check.

    ``getattr``, not attribute access: a module that resolves without
    the attribute degrades to absent. A missing ``render`` used to
    raise AttributeError out of ``print()`` and fail an exec whose only
    crime was printing.
    """
    import importlib

    saved = list(sys.path)
    here = os.getcwd()
    root = _workspace_root(workspace)
    try:
        sys.path = [
            p for p in saved
            if p not in ("", ".", here)
            and (root is None or os.path.realpath(p) != root)
        ]
        mod = importlib.import_module(module)
        origin = getattr(mod, "__file__", None)
        if origin and root and _is_agent_authored(origin, root):
            return None
        return getattr(mod, attr, None)
    except Exception:  # noqa: BLE001 — an absent extension is never fatal
        return None
    finally:
        sys.path = saved


# Resolved renderers, keyed by spec ("" = whatever the default chain
# finds). Cached for the same reason hooks are: the fork template
# outlives one exec.
_RENDERERS: dict[str, Any] = {}

#: The renderer dud ships against when the caller names none. Unlike the
#: outputs hook, dud *defines* this contract — ``render(obj, budget)``
#: exists because ``render_budget`` does — so naming a default
#: implementation of it is ordinary. See DESIGN, "Policy collapses to
#: the image".
_DEFAULT_RENDERER = ("reprobate", "render")


def _renderer(spec: str | None):
    """A budget-controlled repr: the caller's, else reprobate, else none.

    Three steps, best available first, because each is a real
    improvement on the next and none of them is a policy decision an
    agent's output should turn on. ``ping()`` reports which one is
    actually live, so landing a step lower than intended is visible
    rather than a silent difference in what an agent sees.

    Resolved lazily, so an exec that never asks for rendering pays
    nothing, and cached per spec.
    """
    key = spec or ""
    if key not in _RENDERERS:
        resolved = None
        if spec:
            try:
                module, attr = split_hook_spec(spec)
            except ValueError:
                resolved = None
            else:
                resolved = _from_image(module, attr)
        if resolved is None:
            resolved = _from_image(*_DEFAULT_RENDERER)
        _RENDERERS[key] = resolved or False
    return _RENDERERS[key] or None


# Resolved hooks, keyed by spec: the fork template serves many execs and
# a pooled VM can be rebound to a caller naming a different hook.
_OUTPUTS_HOOKS: dict[str, Any] = {}


def split_hook_spec(spec: str) -> tuple[str, str]:
    """``"pkg.module:func"`` -> ``("pkg.module", "func")``.

    Entry-point syntax rather than a bare dotted path, because
    ``a.b.c`` cannot say whether ``b`` is a module or an attribute, and
    guessing means a typo in a package name and a typo in a function
    name produce the same unhelpful failure.
    """
    module, sep, attr = spec.partition(":")
    if not sep or not module or not attr:
        raise ValueError(
            f"outputs_hook must look like 'pkg.module:function', got {spec!r}"
        )
    return module, attr


def _outputs_hook(spec: str | None):
    """The hook the caller named, if the image actually has it.

    Named by the host on ``session(outputs_hook=...)`` rather than
    discovered under a well-known module name. The magic-name version
    of this looked like the renderer's ``reprobate`` and wasn't: dud
    names *reprobate* because dud depends on that library's API, where
    a well-known ``dud_outputs`` would have been dud claiming a global
    module namespace on the consumer's behalf — one owner ever, nothing
    in the API hinting the mechanism exists, and a typo indistinguishable
    from "no hook wanted".

    Absent is still not fatal: an exec whose hook failed to import
    behaves exactly like one with no hook, and ``ping()`` is where the
    difference is visible.
    """
    if not spec:
        return None
    if spec not in _OUTPUTS_HOOKS:
        try:
            module, attr = split_hook_spec(spec)
        except ValueError:
            _OUTPUTS_HOOKS[spec] = False
        else:
            _OUTPUTS_HOOKS[spec] = _from_image(module, attr) or False
    return _OUTPUTS_HOOKS[spec] or None


def _meta_for(obj: Any) -> dict:
    meta: dict[str, Any] = {"type": type(obj).__name__}
    shape = getattr(obj, "shape", None)
    if shape is not None:
        try:
            meta["shape"] = list(shape)
        except TypeError:
            pass
    elif hasattr(obj, "__len__"):
        try:
            meta["len"] = len(obj)
        except TypeError:
            pass
    return meta


class PrintCapture:
    """Collects prints as structured entries under resource guards.

    The guards are not an observation budget — deciding what a model
    should see belongs to the host, which knows the budget, the model
    and the turn. These exist so a runaway print loop cannot flood the
    channel or balloon a supervisor that is PID 1 on a VM rung, and are
    set high enough that they should never be what shapes an
    observation. See DESIGN.md, "Prints: raw material".
    """

    def __init__(self, stdout: io.StringIO, entry_cap: int,
                 max_entries: int, total_cap: int,
                 render_budget: int | None = None,
                 render_hook: str | None = None):
        self.stdout = stdout
        # "pkg.module:function" the caller named, or None for the
        # default chain. Carried rather than resolved here: an exec
        # that never renders must not pay for the import.
        self.render_hook = render_hook
        self.entry_cap = entry_cap
        self.max_entries = max_entries
        # Per-entry render budget, supplied by the host. Absent means
        # plain str(): dud does not invent an observation size, it only
        # applies one — see `render_budget` on HostSession.python.
        self.render_budget = render_budget
        # Without a total, the real ceiling is entry_cap * max_entries,
        # which multiplies into hundreds of MB at guard-sized values.
        self.total_cap = total_cap
        self.entries: list[dict] = []
        self.dropped = 0
        self._total = 0

    def _add(self, text: str, meta: dict, echo: bool = False,
             elided: bool = False) -> None:
        # Against the REMAINING budget, not the running total: checking
        # before the append lets each entry overshoot by its own length,
        # so a single oversized print would sail past any total. The
        # guard has to bound one exec to be worth lowering.
        remaining = self.total_cap - self._total
        if len(self.entries) >= self.max_entries or remaining <= 0:
            self.dropped += 1
            return
        limit = min(self.entry_cap, remaining)
        truncated = len(text) > limit
        text = text[:limit]
        entry = {"text": text, "truncated": truncated, **meta}
        if echo:
            entry["echo"] = True
        if elided:
            # Structural elision, not a mid-token cut: the host can tell
            # which kind of shortening it got, and re-trimming an elided
            # render still beats chopping a raw str().
            entry["elided"] = True
        self.entries.append(entry)
        self._total += len(text)

    def _render(self, objs: tuple, sep: str, plain: str) -> tuple[str, bool]:
        """Entry text for these objects: (text, was_elided).

        Only the ENTRY is rendered. The transcript keeps what real
        Python printed — fidelity is the whole thesis, so `print(df)`
        must produce what a real machine produces, and the structured
        stream is where a summary belongs (the Jupyter split).
        """
        try:
            render = (_renderer(self.render_hook)
                      if self.render_budget else None)
            if render is None or not objs:
                return plain, False
            budget, parts, used = self.render_budget, [], 0
            for i, obj in enumerate(objs):
                remaining = budget - used
                if remaining <= 0:
                    parts.append(f"...{len(objs) - i} more")
                    break
                # A share of what is LEFT, not a fixed floor per
                # argument: a floor multiplies by the argument count, so
                # print(*range(100)) ran ~5x past the caller's number and
                # then met the entry guard as a mid-token cut — the exact
                # thing rendering exists to avoid.
                # Floored so each piece stays legible (a fair share of a
                # small budget across many args renders every int as
                # "."), then clamped to what's left so the floor can
                # never be what multiplies. Args past the budget are
                # counted, not rendered.
                share = min(remaining,
                            max(_MIN_ARG_BUDGET,
                                remaining // (len(objs) - i)))
                piece = render(obj, budget=share)
                parts.append(piece)
                used += len(piece) + len(sep)
            return sep.join(parts), True
        except Exception:  # noqa: BLE001 — a repr of agent data, never fatal
            return plain, False

    def print_fn(self, *args, sep=" ", end="\n", file=None, flush=False):
        text = sep.join(str(a) for a in args)
        target = file if file is not None else self.stdout
        try:
            target.write(text + end)
        except Exception:  # noqa: BLE001 — `file=` is an arbitrary agent object
            pass
        if file is None or file is self.stdout:
            meta = _meta_for(args[0]) if len(args) == 1 else {"type": "tuple"}
            entry_text, elided = self._render(args, sep, text)
            self._add(entry_text, meta, elided=elided)

    def echo(self, value: Any) -> None:
        if value is None:
            return
        text = repr(value)
        self.stdout.write(text + "\n")
        entry_text, elided = self._render((value,), " ", text)
        self._add(entry_text, _meta_for(value), echo=True, elided=elided)


def _split_echo(code: str) -> tuple[ast.Module, ast.Expression | None]:
    tree = ast.parse(code, filename=_RUNNER_FILE)
    last = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        expr = tree.body.pop()
        last = ast.Expression(expr.value)
        ast.copy_location(last, expr)
        ast.fix_missing_locations(last)
    return tree, last


def _clean_traceback(exc: BaseException) -> str:
    parts = traceback.format_exception(type(exc), exc, exc.__traceback__)
    # Drop runner-internal frames; keep from the session file onward.
    out, keep = [parts[0]], False
    for p in parts[1:]:
        if _RUNNER_FILE in p:
            keep = True
        if keep or p is parts[-1]:
            out.append(p)
    return "".join(out) if keep else "".join(parts[:1] + parts[-1:])


def _offer_outputs(harvest: dict, spec: str | None) -> dict:
    """Offer the harvested bindings to the image's outputs hook.

    Live objects that can't cross the codec get serialized guest-side
    into workspace files, which ride back as ordinary diff writes; what
    the hook doesn't claim still harvests through to the host.
    Serializing has to happen here because it needs the live object —
    the same physical reason rendering does.

    The hook receives **every** top-level binding and returns the names
    it fully consumed, which dud drops. It may also rewrite a binding
    in place, which is how a convention like ``ui = {...}`` — write
    some of the dict to files, let the rest cross — is expressed
    without dud knowing the word ``ui``.

    That generality is the point. An earlier cut of this passed only a
    binding literally named ``ui``, which moved the *formats* out of
    dud while leaving the *vocabulary* in: dud would still have known
    what the layer above calls its output dict, and a second consumer
    wanting a different name, or a bare top-level figure, would have
    had to adopt someone else's word for it. Now dud names nothing.

    A hook that raises is treated as having handled nothing. It runs
    third-party serializers over agent data, so it will raise
    eventually, and an exec must not fail because a chart could not be
    written.
    """
    flatten = _outputs_hook(spec)
    if flatten is None or not harvest:
        return harvest
    workspace = os.environ.get("DUD_WORKSPACE") or os.getcwd()
    # The hook is handed a COPY. It may rewrite bindings in place, and
    # a hook that rewrites one and then raises on the next would
    # otherwise leave those edits in the dict we fall back to — so
    # "treated as having handled nothing" would quietly not be true.
    # Shallow, deliberately: a hook mutating a nested container it was
    # handed is editing the agent's own object, which is no more
    # rollback-able than the files it already wrote.
    offered = dict(harvest)
    try:
        handled = set(flatten(offered, workspace) or ())
    except Exception:  # noqa: BLE001 — a hook over agent data, never fatal
        return harvest
    return {k: v for k, v in offered.items() if k not in handled}


def run(channel: Channel, req: dict) -> dict:
    code = req["code"]
    caps = req.get("caps", {})
    stdout_cap = int(caps.get("stdout", _CAP_STDOUT))
    entry_cap = int(caps.get("entry", _CAP_ENTRY))
    max_entries = int(caps.get("entries", _CAP_ENTRIES))
    total_cap = int(caps.get("total", _CAP_TOTAL))
    render_budget = req.get("render_budget")
    render_budget = int(render_budget) if render_budget else None

    # Workspace-root imports, cwd-independent: filesystem modules resolve
    # from the workspace root (`import app.api...` works after `cd app`),
    # matching the VFS executors' documented contract ("imports resolve
    # from '/'"). The runner's cwd stays on sys.path behind it.
    workspace = os.environ.get("DUD_WORKSPACE")
    if workspace and workspace not in sys.path:
        sys.path.insert(0, workspace)

    # Captured before redirect_stdout swaps it out: this is the fd the
    # supervisor is reading, and the only way anything escapes a kill.
    stdout_buf = _TeeBuffer(sys.stdout)
    prints = PrintCapture(stdout_buf, entry_cap, max_entries, total_cap,
                          render_budget, req.get("render_hook"))

    g: dict[str, Any] = {"__name__": "__dud__", "__builtins__": __builtins__}
    injected = {"__name__", "__builtins__", "print", "cache", "emit"}
    g["print"] = prints.print_fn
    cache = CacheView(channel, readonly=bool(req.get("cache_readonly")))
    g["cache"] = cache

    def emit(name: str, value: Any = None) -> None:
        """Fire a structured output at the host (DESIGN.md: emits)."""
        channel.request("emit", {"name": str(name), "value": encode_value(value)})

    g["emit"] = emit
    for name in req.get("host_objects", []):
        g[name] = HostProxy(name, channel)
        injected.add(name)
    inputs = decode_map(req.get("inputs", {}))
    g.update(inputs)
    injected.update(inputs)

    ok, error = True, None
    try:
        body, last = _split_echo(code)
    except SyntaxError as e:
        ok = False
        error = {"etype": "SyntaxError", "message": str(e),
                 "traceback": "".join(traceback.format_exception_only(type(e), e))}
        body = last = None

    if ok:
        try:
            with redirect_stdout(stdout_buf), redirect_stderr(stdout_buf):
                exec(compile(body, _RUNNER_FILE, "exec"), g)  # noqa: S102
                if last is not None:
                    prints.echo(eval(compile(last, _RUNNER_FILE, "eval"), g))  # noqa: S307
        except BaseException as e:  # noqa: BLE001 — report to host, don't die silently
            ok = False
            error = {"etype": type(e).__name__, "message": str(e),
                     "traceback": _clean_traceback(e)}

    outputs, skipped = ({}, {})
    if ok:
        harvest = {
            k: v for k, v in g.items()
            if not k.startswith("_") and k not in injected
        }
        outputs, skipped = encode_map(
            _offer_outputs(harvest, req.get("outputs_hook"))
        )

    transcript = stdout_buf.getvalue()
    if len(transcript) > stdout_cap:
        transcript = transcript[:stdout_cap] + f"\n… [truncated at {stdout_cap} chars]"

    result: dict[str, Any] = {
        "ok": ok, "transcript": transcript,
        "prints": prints.entries, "prints_dropped": prints.dropped,
        "outputs": outputs, "outputs_skipped": skipped,
    }
    if error:
        result["error"] = error
    if ok:
        writes, deletes = cache.flush()
        # Keys in the body, blobs in binary frames, matched by ORDER.
        # `_bins` is an out-of-band slot the transport pops before the
        # body is serialized — carrying them this way instead of
        # widening every return between here and the host keeps the
        # supervisor's timeout, crash and fork-retry paths untouched,
        # which is the code least worth disturbing for a payload
        # optimization.
        result["cache_writes"] = list(writes)
        result["cache_deletes"] = deletes
        result[_BINS] = list(writes.values())
    return result


def serve(sock) -> None:
    """One-request lifecycle over an already-open socket. Split from
    main() so the view-worker template (dud.guest.template) can serve
    the identical contract from a forked child."""
    channel = Channel(sock)

    # Single request lifecycle: read the run request, execute, respond.
    msg, _bins = channel._recv_msg()
    if msg.get("kind") != "req" or msg.get("verb") != "run":
        # Not an assert: protocol validation must survive python -O.
        channel._send_msg(
            {"id": msg.get("id", 0), "kind": "err", "etype": "ProtocolError",
             "message": f"runner expected a run request, got {msg!r}"},
            [],
        )
        channel.close()
        return
    try:
        result = run(channel, msg.get("body", {}))
        bins = result.pop(_BINS, [])
        channel._send_msg({"id": msg["id"], "kind": "resp", "body": result}, bins)
    except Exception as e:  # noqa: BLE001
        channel._send_msg(
            {"id": msg["id"], "kind": "err", "etype": type(e).__name__,
             "message": str(e)},
            [],
        )
    finally:
        channel.close()


def main() -> None:
    import socket as socketlib

    serve(socketlib.socket(fileno=int(sys.argv[1])))


if __name__ == "__main__":
    main()
