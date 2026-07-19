"""Shared host half of a session: the protocol and the public API.

Every rung's Session is the same thing above the transport — it answers
the guest's reverse requests (cache reads, hostcalls, emits), applies
cache write-backs from successful execs, and exposes push/exec/diff. Only
*how the channel is established and torn down* differs per backend. That
lives in the subclass; keeping the rest here is what stops the rungs from
quietly diverging (the ladder's whole invariant).

A subclass sets ``self._ch`` to a live :class:`~dud.proto.Channel` whose
handler is ``self._handle`` and completes the ``hello`` exchange, then
implements :meth:`close`.
"""

from __future__ import annotations

import base64
import io
import posixpath
import tarfile
import time
from pathlib import Path
from typing import Any, Callable

from ..proto import Channel, ChannelClosed, ProtocolError
from ..results import Diff, ExecError, PythonResult, ShellResult
from ..values import decode_map, decode_value, encode_value


class SessionLost(RuntimeError):
    """The guest went away mid-request (VM died, channel EOF/reset).

    The session object is unusable afterward. Recovery is the owner's
    move — dud never holds the authoritative workspace tree, so only
    the layer above can reopen a session and re-push state (see the
    disposable thesis: any VM may vanish at any moment; DudExecutor's
    recovery path is acquire + push + retry-once). Raised in place of
    the transport-level errors so consumers write one ``except``, not
    a taxonomy of socket failures.
    """


def _safe_diff_path(name: str) -> str:
    """Normalize a guest-supplied diff path; fail loud on escapes.

    Diff keys flow into consumer stores and filesystems — making the
    wire shape trustworthy here beats re-checking it in every consumer.
    """
    p = posixpath.normpath(name).lstrip("/")
    if p in ("", ".", "..") or p.startswith("../"):
        raise ProtocolError(f"guest diff path escapes the workspace: {name!r}")
    return p


class HostSession:
    """Backend-agnostic host session. Subclasses own transport + close.

    - ``cache``: dict[str, bytes] of opaque pickled values (guest-side
      pickles). Mutations land only after a successful exec.
    - ``host_objects``: name -> live object; guests reach them solely via
      hostcall. ``allow`` maps name -> permitted method names (default:
      all public callables — rung-1 cooperative posture).
    - ``on_emit``: callback(name, value) for guest emits; also collected
      in ``self.emits``. Emits are *events*, not state: they arrive live
      mid-exec and are kept even when the exec later fails — unlike
      cache writes, which roll back. Consumers must not assume
      checkpoint atomicity for emits.
    """

    _ch: Channel

    def __init__(
        self,
        host_objects: dict[str, Any] | None = None,
        allow: dict[str, set[str]] | None = None,
        cache: dict[str, bytes] | None = None,
        on_emit: Callable[[str, Any], None] | None = None,
    ):
        self.cache: dict[str, bytes] = cache if cache is not None else {}
        self.host_objects = host_objects or {}
        self.allow = allow or {}
        self.emits: list[tuple[str, Any]] = []
        self.on_emit = on_emit
        self._closed = False
        # Liveness bookkeeping (read by VmPool's demand-driven reclaim):
        # a bound VM with _in_flight == 0 is reclaimable, LRU by
        # last_used. Maintained by _request, the single wire seam.
        self._in_flight = 0
        self.last_used = time.monotonic()

    def _request(
        self, verb: str, body: dict | None = None, bins: list[bytes] | None = None
    ) -> tuple[dict, list[bytes]]:
        """The one wire seam: every host->guest request goes through
        here so activity tracking and death detection can't drift per
        call site. Transport failures become :class:`SessionLost`;
        guest-answered errors (``RemoteError``) pass through untouched —
        an answering guest is alive."""
        self.last_used = time.monotonic()
        self._in_flight += 1
        try:
            return self._ch.request(verb, body, bins)
        except (ChannelClosed, OSError) as e:
            raise SessionLost(
                f"guest lost during {verb!r}: {e or type(e).__name__}"
            ) from e
        finally:
            self._in_flight -= 1
            self.last_used = time.monotonic()

    # ---- guest-initiated services -------------------------------------

    def _handle(self, verb: str, body: dict, bins: list[bytes]):
        if verb == "cache.get":
            key = body["key"]
            if key in self.cache:
                return {"hit": True,
                        "b64": base64.b64encode(self.cache[key]).decode()}, []
            return {"hit": False}, []
        if verb == "cache.keys":
            return {"keys": sorted(self.cache)}, []
        if verb == "hostcall":
            return self._hostcall(body), []
        if verb == "emit":
            name = body.get("name", "")
            value = decode_value(body.get("value", {"t": "json", "v": None}))
            self.emits.append((name, value))
            if self.on_emit:
                self.on_emit(name, value)
            return {}, []
        raise ValueError(f"unknown guest verb {verb!r}")

    def _hostcall(self, body: dict) -> dict:
        name, method = body.get("obj", ""), body.get("method", "")
        if name not in self.host_objects:
            raise PermissionError(f"no host object {name!r}")
        allowed = self.allow.get(name)
        if allowed is not None and method not in allowed:
            raise PermissionError(f"{name}.{method} is not allowlisted")
        if method.startswith("_"):
            raise PermissionError(f"{name}.{method}: private methods are never callable")
        target = getattr(self.host_objects[name], method, None)
        if not callable(target):
            raise AttributeError(f"{name}.{method} is not a callable method")
        args = [decode_value(a) for a in body.get("args", [])]
        kwargs = decode_map(body.get("kwargs", {}))
        result = target(*args, **kwargs)
        if result is None:
            return {}
        return {"result": encode_value(result)}

    # ---- host API ------------------------------------------------------

    def push_tree(self, tar_bytes: bytes) -> None:
        self._request("push_tree", {}, [tar_bytes])

    def push_dir(self, path: str | Path) -> None:
        buf = io.BytesIO()
        # Plain tar: the wire is a local socket, so gzip buys nothing and
        # dominates push time ~4:1 at scale (measured: 200 MB tree, 1.5 s
        # of gzip vs 0.4 s for everything else). Extract auto-detects, so
        # compressed producers remain compatible.
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for p in sorted(Path(path).rglob("*")):
                if p.is_file() and not p.is_symlink():
                    tf.add(p, arcname=str(p.relative_to(path)), recursive=False)
        self.push_tree(buf.getvalue())

    def shell(self, script: str, timeout: float = 30.0) -> ShellResult:
        body, _ = self._request(
            "exec_shell", {"script": script, "timeout": timeout}
        )
        return ShellResult(
            transcript=body["transcript"], exit_code=body["exit_code"],
            cwd=body["cwd"], timed_out=body.get("timed_out", False),
        )

    def python(
        self,
        code: str,
        inputs: dict[str, Any] | None = None,
        timeout: float = 30.0,
        caps: dict[str, int] | None = None,
        cache_readonly: bool = False,
        fs_readonly: bool = False,
    ) -> PythonResult:
        """Execute code in a fresh guest runner.

        ``fs_readonly`` asks the guest for a read-only workspace window
        for this exec (view semantics). On overlay staging (VM rungs)
        that's a real remount — writes fail inside the exec; on scan
        staging it's unenforced (rung-1 documented gap), so consumers
        should keep a post-hoc diff check where it matters.
        """
        enc_inputs = {}
        if inputs:
            for k, v in inputs.items():
                enc_inputs[k] = encode_value(v)
        body, _ = self._request(
            "exec_python",
            {"code": code, "inputs": enc_inputs, "timeout": timeout,
             "caps": caps or {}, "host_objects": sorted(self.host_objects),
             "cache_readonly": cache_readonly, "fs_readonly": fs_readonly},
        )
        if body.get("ok"):
            for k, b64 in body.get("cache_writes", {}).items():
                self.cache[k] = base64.b64decode(b64)
            for k in body.get("cache_deletes", []):
                self.cache.pop(k, None)
        err = body.get("error")
        return PythonResult(
            ok=bool(body.get("ok")),
            transcript=body.get("transcript", ""),
            prints=body.get("prints", []),
            prints_dropped=int(body.get("prints_dropped", 0)),
            outputs=decode_map(body.get("outputs", {})),
            outputs_skipped=body.get("outputs_skipped", {}),
            error=ExecError(**err) if err else None,
        )

    def diff(self, rebase: bool = False) -> Diff:
        body, bins = self._request("pull_diff", {"rebase": rebase})
        writes: dict[str, bytes] = {}
        if bins and bins[0]:
            with tarfile.open(fileobj=io.BytesIO(bins[0]), mode="r:*") as tf:
                for member in tf.getmembers():
                    if member.isfile():
                        f = tf.extractfile(member)
                        if f is not None:
                            writes[_safe_diff_path(member.name)] = f.read()
        deletes = [_safe_diff_path(d) for d in body.get("deletes", [])]
        return Diff(writes=writes, deletes=deletes)

    def reset(self) -> None:
        self._request("reset_stage")

    def ping(self) -> dict:
        body, _ = self._request("ping")
        return body

    def close(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def __enter__(self) -> "HostSession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
