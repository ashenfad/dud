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

import inspect
import io
import json
import logging
import posixpath
import tarfile
import time
from pathlib import Path
from typing import Any, Callable

from ..errors import PolicyError
from ..errors import SessionLost  # noqa: F401 — canonical home is dud.errors
from ..proto import Channel, ChannelClosed, ProtocolError
from ..results import Diff, ExecError, PythonResult, ShellResult
from ..values import decode_map, decode_value, encode_value

# Denials are policy events, not noise: a guest reaching for something
# outside its allowlist is exactly what an embedder wants to see.
_log = logging.getLogger(__name__)

# What an ordinary file gets when nothing says otherwise. Diff.modes
# records only departures from it, so the map stays empty for the
# overwhelmingly common case and consumers that ignore it are unchanged.
_DEFAULT_FILE_MODE = 0o644

# ---- host-side deadlines ------------------------------------------------
#
# Death recovers on its own (the channel closes and the owner
# re-acquires); a HANG does not, and without a deadline a wedged guest
# blocks its host forever. This was open for a while on the theory that
# it needed "a single number" that could cover both a ping and a
# push_tree of a 200 MB tree. It doesn't: those differ by orders of
# magnitude, and the host already knows which verb it is sending and how
# big the payload is. So the budget is per verb, derived where the size
# is knowable and fixed where it isn't.
#
# Every value here is a CEILING on a wedged guest, not a service-level
# expectation — the operations themselves are milliseconds. They are set
# where a human would rather wait than see a false failure, because the
# cost of being wrong in one direction is a spurious SessionLost on a
# healthy session and in the other is a hang that already lasted forever.

# Execs carry their own timeout, which the guest enforces. The host waits
# for that plus the guest's reporting tail: it kills at the timeout,
# drains the dying runner's pipe (0.5 s), and reaps (up to 5 s).
_EXEC_SLACK = 15.0

# push_tree extract cost scales with the tree. Deliberately pessimistic
# — an order of magnitude under what a local socket onto tmpfs actually
# does — because overshooting only delays a failure nobody is waiting on.
_PUSH_FLOOR = 60.0
_PUSH_BYTES_PER_SEC = 10 * 1024 * 1024

_VERB_BUDGETS = {
    "ping": 30.0,
    "shutdown": 15.0,
    "resync": 30.0,
    "reset_stage": 60.0,
    # Bounded guest-side by a 2 s kill sweep, then a full-tree wipe.
    "reset_guest": 120.0,
    # os.sync() on a guest that may have just written a large scratch
    # volume. The snapshot itself is the VMM's problem, not this verb's.
    "freeze": 120.0,
    # O(changes), so normally ~1 ms — but the size isn't knowable in
    # advance the way push_tree's is, so the ceiling is generous.
    "pull_diff": 300.0,
}
_DEFAULT_BUDGET = 60.0


def _budget_for(verb: str, body: dict | None, bins: list[bytes] | None) -> float:
    """Wall-clock budget for one host->guest request."""
    if verb in ("exec_python", "exec_shell"):
        return float((body or {}).get("timeout", 30.0)) + _EXEC_SLACK
    if verb == "push_tree":
        nbytes = sum(len(b) for b in bins or ())
        return _PUSH_FLOOR + nbytes / _PUSH_BYTES_PER_SEC
    return _VERB_BUDGETS.get(verb, _DEFAULT_BUDGET)


def public_methods(obj: Any) -> frozenset[str]:
    """Every public callable on ``obj``, as a concrete set.

    The honest way to say "expose this whole object"::

        allow={"db": dud.public_methods(my_db)}

    Deliberately a helper rather than a wildcard. A wildcard would put a
    permissive branch back into :meth:`HostSession._hostcall` — the one
    thing the fail-closed allowlist exists to remove — and it would be
    the easiest thing to type, which is how the old permissive default
    became what everyone shipped. A resolved set costs one call and
    keeps three properties a wildcard can't:

    - the gate stays a plain membership test, with nothing to audit;
    - ``session.allow`` stays data you can print, log, and assert on;
    - it snapshots *now*, so a method added later by a plugin or a
      monkeypatch is not granted retroactively.

    Reads attributes statically: a ``@property`` whose getter opens a
    connection must not fire merely because someone asked what this
    object exposes.
    """
    names = set()
    for name in dir(obj):
        if name.startswith("_"):
            continue  # never callable over hostcall anyway
        try:
            attr = inspect.getattr_static(obj, name)
        except AttributeError:
            continue
        if isinstance(attr, (classmethod, staticmethod)):
            # Static lookup hands back the descriptor, and a bare
            # `classmethod` object is not itself callable — so testing
            # it directly would drop a perfectly ordinary public method
            # from a helper whose contract is "all of them". Unwrapping
            # __func__ invokes nothing, so the property guarantee holds.
            attr = attr.__func__
        if callable(attr):
            names.add(name)
    return frozenset(names)


def _clean_methods(name: str, value: Any) -> frozenset[str]:
    """One allow entry, validated and frozen into a set of names."""
    if isinstance(value, (str, bytes)):
        # `allow={"db": "query"}` — the braces got dropped. Left alone
        # this silently becomes a SUBSTRING match, so a one-character
        # typo quietly widens the grant (`db.q` passes `"q" in "query"`).
        raise PolicyError(
            f"allow[{name!r}] is a string; it must be a set of method "
            f"names. Did you mean {{{value!r}}}? A bare string matches "
            f"by substring, which would allow more than it names."
        )
    try:
        methods = frozenset(value)
    except TypeError:
        raise PolicyError(
            f"allow[{name!r}] must be a set of method names, got "
            f"{type(value).__name__}"
        ) from None
    bad = [m for m in methods if not isinstance(m, str)]
    if bad:
        # Sort the reprs, not the values: {7, None} has no ordering, and
        # a TypeError here would escape the PolicyError contract that
        # callers of this function are told to rely on.
        raise PolicyError(
            f"allow[{name!r}] contains non-string method names: "
            f"{', '.join(sorted(map(repr, bad)))}"
        )
    return methods


def require_allowlist(
    host_objects: dict[str, Any] | None,
    allow: dict[str, Any] | None,
) -> dict[str, frozenset[str]]:
    """Refuse a host object that has no ``allow`` entry; return the
    normalized allowlist.

    Fail closed, like every other policy decision here (a rung the host
    can't provide raises rather than degrading). The allowlist is the
    *only* fine-grained gate between agent code and a live host object,
    so "unspecified" cannot mean "everything" — that was the one place
    dud failed open, and the default is what most callers ship.

    ``allow={"db": set()}`` is a legitimate registration with no
    callable methods: explicitly nothing, which is the point. Entries
    naming objects that aren't registered are left alone, so one policy
    dict can be shared across sessions that expose different subsets —
    though they are still checked, because a malformed entry is a bug
    wherever it sits.

    Normalizing to frozensets is what makes the gate's membership test
    mean what it looks like. It also closes an accidental escape hatch:
    any object with a ``__contains__`` that answered True used to pass
    every method, invisibly and unauditably.
    """
    for name in host_objects or {}:
        if name not in (allow or {}):
            raise PolicyError(
                f"host object {name!r} was registered without an allow "
                f"entry. Pass allow={{{name!r}: {{'method', ...}}}} naming "
                f"the methods guest code may call, "
                f"allow={{{name!r}: dud.public_methods(obj)}} for all of "
                f"them, or {{{name!r}: set()}} for none. dud will not "
                f"infer a policy for a live host object."
            )
    clean = {n: _clean_methods(n, v) for n, v in (allow or {}).items()}
    unknown = sorted(set(clean) - set(host_objects or {}))
    if unknown:
        # Not an error: a shared policy dict outliving any one session's
        # object set is a reasonable pattern. Still the first thing to
        # check when an allowlist appears not to apply.
        _log.debug("allow entries for unregistered host objects: %s",
                   ", ".join(unknown))
    return clean


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
      hostcall. ``allow`` maps name -> permitted method names and is
      **required** for every registered object — see
      :func:`require_allowlist`. ``allow={"db": set()}`` registers one
      with no callable methods.
    - ``outputs_hook``: ``"pkg.module:function"`` in the guest image,
      handed every harvested binding so it can serialize what the codec
      can't carry into workspace files. dud supplies no default and
      knows no format; ``ping()["outputs_hook"]`` reports whether the
      named hook resolved.
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
        outputs_hook: str | None = None,
    ):
        self.cache: dict[str, bytes] = cache if cache is not None else {}
        # "pkg.module:function" naming a hook the IMAGE provides, which
        # the guest offers every harvested binding to (see the runner's
        # _offer_outputs). Host-side config rather than a well-known
        # module name so the mechanism is visible in this signature,
        # and so a typo is something ping() can show you instead of
        # looking identical to wanting no hook at all.
        self.outputs_hook = outputs_hook
        self.host_objects = host_objects or {}
        self.allow = require_allowlist(host_objects, allow)
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
        an answering guest is alive.

        "Transport failure" includes a channel torn by a *concurrent*
        speaker, not just one that died. ``VmPool._make_room``
        deliberately reclaims a bound session whose ``_in_flight`` is 0
        and accepts racing the owner's next call, so two threads can
        end up inside ``Channel.request`` on one socket: each
        ``_recv_msg`` loop may consume frames the other issued, which
        surfaces as a foreign response id (``ProtocolError``) or as a
        binary payload parsed as a header (a JSON/UTF-8 decode error).
        None of those are wire *bugs* — they are the documented reclaim
        landing mid-call — and the owner is told to recover by catching
        ``SessionLost``, so they have to arrive as one. Decode failures
        are named precisely rather than caught as ``ValueError``: a
        caller handing us an unserializable ``body`` also raises
        ValueError, and that is a bug in the call, not a dead guest.

        That race has a fourth outcome which is **not** an exception: if
        the split leaves a reader taking the first four bytes of a JSON
        header as a length prefix, ``_recv_exact`` blocks on a bogus
        multi-gigabyte read, and no translation catches a hang. Both
        halves of the answer to that are now in place — the per-verb
        deadline below bounds it in general, and ``VmPool._teardown``
        aborts the socket of a reclaimed session specifically."""
        if getattr(self, "frozen", False):
            raise SessionLost(
                f"session is frozen (parked as a snapshot); "
                f"call thaw() before {verb!r}"
            )
        self.last_used = time.monotonic()
        self._in_flight += 1
        budget = _budget_for(verb, body, bins)
        try:
            return self._ch.request(verb, body, bins, timeout=budget)
        except TimeoutError as e:
            # Checked before the OSError arm below, which it subclasses.
            # Worth its own message: every other cause here is a guest
            # that went away, and "did not answer in time" is a guest
            # that is still there and wedged — a different bug to chase.
            raise SessionLost(
                f"guest did not answer {verb!r} within {budget:.0f}s"
            ) from e
        except (ChannelClosed, OSError, ProtocolError,
                json.JSONDecodeError, UnicodeDecodeError) as e:
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
                # A binary frame, not base64 in the body. Cache values
                # are opaque pickles of unbounded size, and base64 put
                # a 1.33x string through a JSON encode here, a parse in
                # the supervisor forwarding it, and a decode in the
                # runner — three transient copies of a payload that
                # never needed to be text. The frame just rides along:
                # _pump_runner already forwards bins for reverse
                # requests, so nothing between here and the runner
                # changes.
                return {"hit": True}, [self.cache[key]]
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
            raise self._denied(f"no host object {name!r}")
        # `.get(name) or ()` rather than trusting the constructor: these
        # fields are publicly assignable, and a registration mutated in
        # afterwards must land on deny, not on wide open.
        allowed = self.allow.get(name) or ()
        if method not in allowed:
            raise self._denied(f"{name}.{method} is not allowlisted")
        if method.startswith("_"):
            raise self._denied(
                f"{name}.{method}: private methods are never callable"
            )
        target = getattr(self.host_objects[name], method, None)
        if not callable(target):
            raise AttributeError(f"{name}.{method} is not a callable method")
        args = [decode_value(a) for a in body.get("args", [])]
        kwargs = decode_map(body.get("kwargs", {}))
        result = target(*args, **kwargs)
        if result is None:
            return {}
        return {"result": encode_value(result)}

    @staticmethod
    def _denied(reason: str) -> PermissionError:
        """Refuse a hostcall, and say so where an embedder can see it.

        Guest code reaching past its allowlist is the one thing on this
        boundary worth an unprompted look, whether it's a bug or an
        agent probing. ``PermissionError`` stays the raised type: it
        crosses to the guest as a RemoteError and consumers already
        catch it.
        """
        _log.warning("hostcall denied: %s", reason)
        return PermissionError(reason)

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
        render_budget: int | None = None,
    ) -> PythonResult:
        """Execute code in a fresh guest runner.

        ``fs_readonly`` asks the guest for a read-only workspace window
        for this exec (view semantics). On overlay staging (VM rungs)
        that's a real remount — writes fail inside the exec; on scan
        staging it's unenforced (rung-1 documented gap), so consumers
        should keep a post-hoc diff check where it matters.

        ``caps`` are resource guards on what an exec may send back —
        ``stdout`` (transcript), ``entry`` (one print), ``entries``
        (count), ``total`` (across entries). They exist to stop a
        runaway print loop flooding the channel, not to size an
        observation: choosing what a model should see is the caller's
        job, and it gets every entry plus its metadata to do it with.
        The defaults sit far above any plausible observation, so raising
        them is rarely the answer; lowering them is a way to bound a
        specific untrusted exec.

        ``render_budget`` asks the guest to render each print entry to
        roughly that many characters using structural elision
        (``[1, 2, 3, ...86 more]``) instead of a mid-token cut. It is
        the one piece of observation shaping that *has* to happen
        guest-side, because it needs the live object — a DataFrame's
        head/tail cannot be reconstructed from a chopped string. The
        number stays the caller's: dud never invents one, so leaving
        this unset means plain ``str()``.

        Requires ``reprobate`` in the image (``packages=["reprobate"]``);
        without it entries fall back to plain text, and ``ping()``
        reports which renderer is live so the difference is visible
        rather than silent. Rendered entries are marked ``elided``.
        The transcript is never rendered — it keeps exactly what real
        Python printed.
        """
        enc_inputs = {}
        if inputs:
            for k, v in inputs.items():
                enc_inputs[k] = encode_value(v)
        body, bins = self._request(
            "exec_python",
            {"code": code, "inputs": enc_inputs, "timeout": timeout,
             "caps": caps or {}, "render_budget": render_budget,
             "host_objects": sorted(self.host_objects),
             "outputs_hook": self.outputs_hook,
             "cache_readonly": cache_readonly, "fs_readonly": fs_readonly},
        )
        if body.get("ok"):
            # Keys in the body, blobs in frames, matched by order —
            # an unbounded pickle never becomes a JSON string.
            for k, blob in zip(body.get("cache_writes", []), bins):
                self.cache[k] = blob
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
        modes: dict[str, int] = {}
        if bins and bins[0]:
            with tarfile.open(fileobj=io.BytesIO(bins[0]), mode="r:*") as tf:
                for member in tf.getmembers():
                    if member.isfile():
                        f = tf.extractfile(member)
                        if f is not None:
                            path = _safe_diff_path(member.name)
                            writes[path] = f.read()
                            # Permission bits only. The guest runs as
                            # root, so a setuid bit here is agent-chosen
                            # and would cross into whatever restores the
                            # diff — masking is the boundary doing its
                            # job, and is why the raw archive is not
                            # exposed anywhere it could be re-extracted.
                            perms = member.mode & 0o777
                            if perms != _DEFAULT_FILE_MODE:
                                modes[path] = perms
        deletes = [_safe_diff_path(d) for d in body.get("deletes", [])]
        return Diff(writes=writes, deletes=deletes, modes=modes)

    def reset(self) -> None:
        self._request("reset_stage")

    def ping(self) -> dict:
        """Guest liveness plus what this image can actually offer.

        ``outputs_hook`` rides along because the hook is host-side
        config the supervisor never sees otherwise, and reporting it is
        the whole reason to name it here: a hook that failed to import
        behaves exactly like no hook at all, so the difference has to
        be visible somewhere. Here.
        """
        body, _ = self._request("ping", {"outputs_hook": self.outputs_hook})
        return body

    def close(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def __enter__(self) -> "HostSession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
