"""HostSession boundary hygiene (no guest process needed)."""

from __future__ import annotations

import json
import socket as socketlib
import struct
from contextlib import contextmanager

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

    def request(self, verb, body=None, bins=None):
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
