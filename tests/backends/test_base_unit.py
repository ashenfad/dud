"""HostSession boundary hygiene (no guest process needed)."""

from __future__ import annotations

import pytest

from dud.backends.base import (
    HostSession,
    SessionLost,
    _safe_diff_path,
    public_methods,
    require_allowlist,
)
from dud.errors import PolicyError
from dud.proto import ChannelClosed, ProtocolError, RemoteError


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
