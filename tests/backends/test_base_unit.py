"""HostSession boundary hygiene (no guest process needed)."""

from __future__ import annotations

import pytest

from dud.backends.base import HostSession, SessionLost, _safe_diff_path
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
