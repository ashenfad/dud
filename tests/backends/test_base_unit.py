"""HostSession boundary hygiene (no guest process needed)."""

from __future__ import annotations

import json
import socket as socketlib
import struct
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from dud.backends.base import (
    HostSession,
    SessionLost,
    _safe_diff_path,
    public_methods,
    require_allowlist,
)
from dud.errors import PolicyError
from dud.proto import Channel, ChannelClosed, ProtocolError, RemoteError


def test_safe_diff_path_passes_normal_paths():
    assert _safe_diff_path("a/b.txt") == "a/b.txt"
    assert _safe_diff_path("./a/./b") == "a/b"


def test_safe_diff_path_normalizes_absolute_to_relative():
    assert _safe_diff_path("/etc/passwd") == "etc/passwd"


def test_safe_diff_path_rejects_traversal():
    for evil in ("../x", "a/../../x", "..", "a/b/../../../c"):
        with pytest.raises(ProtocolError):
            _safe_diff_path(evil)


class _RaisingChannel:
    def __init__(self, exc: Exception):
        self._exc = exc

    def request(self, verb, body=None, bins=None, timeout=None):
        raise self._exc


def _session_with(exc: Exception) -> HostSession:
    s = HostSession()
    s._ch = _RaisingChannel(exc)  # type: ignore[assignment]
    return s


def test_transport_death_becomes_session_lost():
    """One except for consumers: EOF, reset, broken pipe all surface as
    SessionLost with the original as __cause__."""
    for exc in (ChannelClosed(), ConnectionResetError(), BrokenPipeError(),
                OSError("socket gone")):
        s = _session_with(exc)
        with pytest.raises(SessionLost) as ei:
            s.ping()
        assert ei.value.__cause__ is exc
        assert s._in_flight == 0  # bookkeeping unwound


def test_guest_answered_errors_pass_through():
    """A guest that answers is alive: RemoteError is not a death."""
    s = _session_with(RemoteError("exec_python", "boom", "ValueError"))
    with pytest.raises(RemoteError):
        s.ping()


# ---- a channel torn by a concurrent speaker ----------------------------
#
# VmPool._make_room deliberately reclaims a bound session whose
# _in_flight is 0 and accepts racing the owner's next call, so two
# threads can be inside Channel.request on one socket. Each _recv_msg
# loop may then consume frames the other issued. The three tests below
# inject exactly the frames that race produces, deterministically —
# driving live threads would only make the same three outcomes
# probabilistic. All three must reach the owner as SessionLost, because
# that is the one exception the pool's recovery path tells consumers to
# catch.


@contextmanager
def _channel_pair():
    """A real Channel plus the raw peer socket, for injecting frames."""
    a, b = socketlib.socketpair()
    try:
        yield Channel(a), b
    finally:
        a.close()
        b.close()


def _send_frame(sock: socketlib.socket, payload: bytes) -> None:
    sock.sendall(struct.pack(">I", len(payload)) + payload)


def _session_on(ch: Channel) -> HostSession:
    s = HostSession()
    s._ch = ch  # type: ignore[assignment]
    return s


def test_foreign_response_id_becomes_session_lost():
    """The other speaker's response, read by our loop: ProtocolError."""
    with _channel_pair() as (ch, peer):
        s = _session_on(ch)
        _send_frame(peer, json.dumps(
            {"id": 999, "kind": "resp", "body": {}, "nbin": 0}
        ).encode())
        with pytest.raises(SessionLost) as ei:
            s.ping()
        assert isinstance(ei.value.__cause__, ProtocolError)
        assert s._in_flight == 0


def test_unparseable_frame_becomes_session_lost():
    """A binary attachment read as a header: JSON decode failure.

    Text-shaped bytes on purpose — a workspace tar carrying a CSV gets
    past the UTF-8 decode and dies at json.loads, which is the other
    half of the pair the next test covers.
    """
    with _channel_pair() as (ch, peer):
        s = _session_on(ch)
        _send_frame(peer, b"data/in.csv\x00a,b\n1,2\n")
        with pytest.raises(SessionLost) as ei:
            s.ping()
        assert isinstance(ei.value.__cause__, json.JSONDecodeError)
        assert s._in_flight == 0


def test_undecodable_frame_becomes_session_lost():
    """Same, when the stray bytes aren't even UTF-8."""
    with _channel_pair() as (ch, peer):
        s = _session_on(ch)
        _send_frame(peer, b"\xff\xfe\x00\x01")
        with pytest.raises(SessionLost) as ei:
            s.ping()
        assert isinstance(ei.value.__cause__, UnicodeDecodeError)
        assert s._in_flight == 0


def test_handshake_protocol_errors_still_raise():
    """The widening is scoped to _request, not to Channel: a version
    mismatch at connect is a real protocol bug and must stay one — it
    is not a guest that died, and no recovery path should retry it."""
    with _channel_pair() as (ch, peer):
        _send_frame(peer, json.dumps({"kind": "hello", "proto": 999}).encode())
        with pytest.raises(ProtocolError, match="version mismatch"):
            ch.hello_recv()


# ---- per-verb deadlines ------------------------------------------------


def test_budget_scales_with_the_exec_timeout():
    """Execs carry their own timeout; the host waits for that plus the
    guest's reporting tail (kill, drain, reap)."""
    from dud.backends.base import _EXEC_SLACK, _budget_for

    assert _budget_for("exec_python", {"timeout": 5.0}, None) == 5.0 + _EXEC_SLACK
    assert _budget_for("exec_shell", {"timeout": 600.0}, None) == 600.0 + _EXEC_SLACK
    # No explicit timeout: the same 30s default the verbs document.
    assert _budget_for("exec_shell", {}, None) == 30.0 + _EXEC_SLACK


def test_budget_scales_with_the_pushed_tree():
    """A 200 MB push must not share a ping's ceiling — the whole reason
    this is per-verb rather than one number."""
    from dud.backends.base import _PUSH_FLOOR, _budget_for

    assert _budget_for("push_tree", {}, [b""]) == _PUSH_FLOOR
    big = _budget_for("push_tree", {}, [b"x" * (200 * 1024 * 1024)])
    assert big > _PUSH_FLOOR + 15


def test_unknown_verbs_get_a_finite_budget():
    """A verb added later must not silently inherit "wait forever"."""
    from dud.backends.base import _budget_for

    assert _budget_for("some_future_verb", {}, None) > 0


def test_wedged_guest_becomes_session_lost(monkeypatch):
    """A guest that accepts and never answers is the case death recovery
    could not reach: no EOF, so nothing raises until a deadline does."""
    import dud.backends.base as basemod

    monkeypatch.setitem(basemod._VERB_BUDGETS, "ping", 0.25)
    with _channel_pair() as (ch, peer):  # noqa: F841 — peer never answers
        s = _session_on(ch)
        started = time.monotonic()
        with pytest.raises(SessionLost, match="did not answer"):
            s.ping()
        assert time.monotonic() - started < 5.0
        assert s._in_flight == 0


def test_bogus_length_prefix_does_not_hang_forever(monkeypatch):
    """The race outcome that is a hang rather than an exception: a
    length prefix read out of the middle of a payload commits the reader
    to a multi-gigabyte read that never completes."""
    import dud.backends.base as basemod

    monkeypatch.setitem(basemod._VERB_BUDGETS, "ping", 0.25)
    with _channel_pair() as (ch, peer):
        s = _session_on(ch)
        _send_frame(peer, struct.pack(">I", 0xFFFFFF) + b"partial")
        with pytest.raises(SessionLost):
            s.ping()
        assert s._in_flight == 0


def test_handler_time_is_not_charged_to_the_guest():
    """The channel is bidirectional: this loop SERVES the guest's
    reverse requests while awaiting its own response. A slow hostcall is
    our own slowness, and charging it to the guest would time out a
    session whose only fault was calling a slow host object."""
    budget, handler_cost = 0.4, 0.25

    def slow_handler(verb, body, bins):
        time.sleep(handler_cost)
        return {}, []

    with _channel_pair() as (ch, peer):
        ch.handler = slow_handler
        # Two reverse requests, then the real response. Their handler
        # time alone (0.5s) exceeds the budget; only the guest's own
        # idle time should count against it.
        for i in range(2):
            _send_frame(peer, json.dumps(
                {"id": i + 1, "kind": "req", "verb": "emit", "body": {}, "nbin": 0}
            ).encode())
        _send_frame(peer, json.dumps(
            {"id": 1, "kind": "resp", "body": {"pong": True}, "nbin": 0}
        ).encode())
        body, _ = ch.request("ping", {}, None, timeout=budget)
        assert body == {"pong": True}


def test_a_deadline_does_not_outlive_its_request():
    """Timeouts are per request. A socket left armed would apply one
    call's budget to whatever runs next on the channel — including
    serve(), which must never time out."""
    with _channel_pair() as (ch, peer):
        _send_frame(peer, json.dumps(
            {"id": 1, "kind": "resp", "body": {}, "nbin": 0}
        ).encode())
        ch.request("ping", {}, None, timeout=5.0)
        assert ch._sock.gettimeout() is None


def test_guest_side_channels_stay_blocking():
    """No timeout argument means no deadline: the guest imposes none on
    the host, and serve() waits indefinitely by design."""
    with _channel_pair() as (ch, peer):
        _send_frame(peer, json.dumps(
            {"id": 1, "kind": "resp", "body": {}, "nbin": 0}
        ).encode())
        ch.request("ping")
        assert ch._sock.gettimeout() is None


def test_no_host_side_call_site_bypasses_the_wire_seam():
    """`_request` calls itself "the one wire seam: every host->guest
    request goes through here" — and it wasn't. close(), pool release,
    and firecracker freeze/thaw all called `_ch.request` directly, so
    the budgets defined for shutdown/reset_guest/freeze/resync were
    dead config and a guest that wedged during any of them still hung
    forever.

    Pinned by a source scan rather than left to review, because the
    failure is invisible at every call site: the direct call works
    perfectly until the day a guest stops answering.
    """
    import dud.backends as backends_pkg

    pkg = Path(backends_pkg.__file__).parent
    offenders = []
    for py in sorted(pkg.glob("*.py")):
        if py.name == "base.py":  # the seam itself
            continue
        for i, line in enumerate(py.read_text().splitlines(), 1):
            if "_ch.request(" in line:
                offenders.append(f"{py.name}:{i}: {line.strip()}")
    assert not offenders, (
        "host->guest requests must go through HostSession._request so "
        "they get a deadline:\n  " + "\n  ".join(offenders)
    )


def test_body_that_cannot_be_serialized_is_not_a_death():
    """Decode failures are named precisely rather than caught as
    ValueError, so a bug in the CALL doesn't masquerade as a dead
    guest: json.dumps raises ValueError on a circular structure."""
    with _channel_pair() as (ch, peer):  # noqa: F841 — peer never answers
        s = _session_on(ch)
        circular: dict = {}
        circular["self"] = circular
        with pytest.raises(ValueError) as ei:
            s._request("ping", circular)
        assert not isinstance(ei.value, SessionLost)
        assert s._in_flight == 0


# ---- allowlist shape ---------------------------------------------------


class _Obj:
    def query(self):
        return "q"

    def drop_all(self):
        return "dropped"

    @classmethod
    def from_url(cls, url):
        return cls()

    @staticmethod
    def ping():
        return "pong"

    def _secret(self):
        return "s"

    version = "1.0"

    @property
    def connection(self):
        raise AssertionError("a property getter must never fire here")


def test_public_methods_collects_public_callables():
    assert public_methods(_Obj()) == frozenset(
        {"query", "drop_all", "from_url", "ping"}
    )


def test_public_methods_includes_class_and_static_methods():
    """Static lookup hands back the descriptor, and a bare `classmethod`
    object is not itself callable — testing it directly would silently
    drop an ordinary public method from a whole-object grant."""
    granted = public_methods(_Obj())
    assert "from_url" in granted   # classmethod
    assert "ping" in granted       # staticmethod


def test_public_methods_reads_statically():
    """A `@property` whose getter opens a connection must not fire just
    because someone asked what the object exposes."""
    public_methods(_Obj())  # the property above asserts if invoked


def test_public_methods_snapshots_rather_than_tracking():
    """The point of resolving to a set instead of a wildcard: a method
    added later is not granted retroactively."""
    obj = _Obj()
    granted = public_methods(obj)
    _Obj.added_later = lambda self: None
    try:
        assert "added_later" not in granted
        assert "added_later" in public_methods(obj)  # a fresh call sees it
    finally:
        del _Obj.added_later


def test_bare_string_allow_is_rejected():
    """`allow={"db": "query"}` — braces dropped — would otherwise match
    by SUBSTRING, so `db.q` would pass `"q" in "query"`. A one-character
    typo silently widening the grant is exactly what must not happen."""
    with pytest.raises(PolicyError, match="is a string"):
        require_allowlist({"db": _Obj()}, {"db": "query"})


def test_non_iterable_allow_is_rejected():
    with pytest.raises(PolicyError, match="must be a set of method names"):
        require_allowlist({"db": _Obj()}, {"db": 42})


def test_non_string_members_are_rejected():
    with pytest.raises(PolicyError, match="non-string method names"):
        require_allowlist({"db": _Obj()}, {"db": {"query", 7}})


def test_unorderable_bad_members_still_raise_policy_error():
    """{7, None} has no ordering, so sorting the values themselves
    would raise TypeError and escape the PolicyError contract."""
    with pytest.raises(PolicyError, match="non-string method names"):
        require_allowlist({"db": _Obj()}, {"db": {"query", 7, None}})


def test_contains_object_no_longer_grants_everything():
    """An accidental allow-all: any object whose __contains__ answered
    True used to pass every method, invisibly and unauditably."""

    class Anything:
        def __contains__(self, _m):
            return True

    with pytest.raises(PolicyError):
        require_allowlist({"db": _Obj()}, {"db": Anything()})


def test_allowlist_normalizes_to_frozensets():
    """`session.allow` stays data you can print, log and assert on."""
    clean = require_allowlist({"db": _Obj()}, {"db": ["query", "query"]})
    assert clean == {"db": frozenset({"query"})}
    assert isinstance(clean["db"], frozenset)


def test_malformed_entry_is_caught_even_for_an_unregistered_object():
    """Shared policy dicts are legal, but a malformed entry is a bug
    wherever it sits."""
    with pytest.raises(PolicyError):
        require_allowlist({}, {"unused": "oops"})
