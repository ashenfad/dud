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
import base64
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
        body, _ = self._ch.request("cache.get", {"key": key})
        if not body.get("hit"):
            self._known_missing.add(key)
            raise KeyError(key)
        raw = base64.b64decode(body["b64"])
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

    def flush(self) -> tuple[dict[str, str], list[str]]:
        """(writes as b64 pickles, deletes) for the result payload.

        Keys that were only read ship back only if their re-pickled
        bytes differ from what was fetched — that keeps in-place
        mutation capture (``cache["x"].append(...)``) without turning
        every read into a spurious write upstream.
        """
        writes = {}
        for k, v in self._local.items():
            raw = pickle.dumps(v, protocol=pickle.HIGHEST_PROTOCOL)
            if self._fetched.get(k) != raw:
                writes[k] = base64.b64encode(raw).decode()
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
    def __init__(self, stdout: io.StringIO, entry_cap: int, max_entries: int):
        self.stdout = stdout
        self.entry_cap = entry_cap
        self.max_entries = max_entries
        self.entries: list[dict] = []
        self.dropped = 0

    def _add(self, text: str, meta: dict, echo: bool = False) -> None:
        if len(self.entries) >= self.max_entries:
            self.dropped += 1
            return
        truncated = len(text) > self.entry_cap
        entry = {"text": text[: self.entry_cap], "truncated": truncated, **meta}
        if echo:
            entry["echo"] = True
        self.entries.append(entry)

    def print_fn(self, *args, sep=" ", end="\n", file=None, flush=False):
        text = sep.join(str(a) for a in args)
        target = file if file is not None else self.stdout
        try:
            target.write(text + end)
        except Exception:
            pass
        if file is None or file is self.stdout:
            meta = _meta_for(args[0]) if len(args) == 1 else {"type": "tuple"}
            self._add(text, meta)

    def echo(self, value: Any) -> None:
        if value is None:
            return
        text = repr(value)
        self.stdout.write(text + "\n")
        self._add(text, _meta_for(value), echo=True)


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


def _flatten_ui(g: dict) -> None:
    """Materialize rich ``ui`` values to ``/ui`` files in the workspace.

    The ``ui = {...}`` convention: rich live objects (plotly/pandas/etc.)
    can't cross the codec, so serialize them guest-side into workspace
    files (adopted by the host), and drop them from ``ui`` so the
    representable remainder still harvests through to the host renderer.
    """
    ui = g.get("ui")
    if not isinstance(ui, dict) or not ui:
        return
    from .ui import flatten_rich

    workspace = os.environ.get("DUD_WORKSPACE") or os.getcwd()
    handled = flatten_rich(ui, workspace)
    if handled:
        g["ui"] = {k: v for k, v in ui.items() if k not in handled}


def run(channel: Channel, req: dict) -> dict:
    code = req["code"]
    caps = req.get("caps", {})
    stdout_cap = int(caps.get("stdout", 20_000))
    entry_cap = int(caps.get("entry", 2_000))
    max_entries = int(caps.get("entries", 200))

    # Workspace-root imports, cwd-independent: filesystem modules resolve
    # from the workspace root (`import app.api...` works after `cd app`),
    # matching the VFS executors' documented contract ("imports resolve
    # from '/'"). The runner's cwd stays on sys.path behind it.
    workspace = os.environ.get("DUD_WORKSPACE")
    if workspace and workspace not in sys.path:
        sys.path.insert(0, workspace)

    stdout_buf = io.StringIO()
    prints = PrintCapture(stdout_buf, entry_cap, max_entries)

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
        _flatten_ui(g)
        harvest = {
            k: v for k, v in g.items()
            if not k.startswith("_") and k not in injected
        }
        outputs, skipped = encode_map(harvest)

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
        result["cache_writes"] = writes
        result["cache_deletes"] = deletes
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
        channel._send_msg({"id": msg["id"], "kind": "resp", "body": result}, [])
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
