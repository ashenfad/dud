"""Conformance: cache / hostcall / emit — the reverse channel."""

import pickle

import pytest

from dud import Session


def test_cache_write_then_read_across_execs(session):
    r1 = session.python("cache['n'] = 41")
    assert r1.ok
    r2 = session.python("m = cache['n'] + 1")
    assert r2.ok and r2.outputs["m"] == 42


def test_cache_values_are_opaque_bytes_host_side(session):
    session.python("cache['obj'] = {'nested': [1, 2]}")
    assert isinstance(session.cache["obj"], bytes)
    # the host *could* unpickle, but the backend never does
    assert pickle.loads(session.cache["obj"]) == {"nested": [1, 2]}


def test_cache_not_applied_on_error(session):
    session.python("cache['safe'] = 1")
    r = session.python("cache['safe'] = 999\nraise RuntimeError('abort')")
    assert not r.ok
    r2 = session.python("v = cache['safe']")
    assert r2.outputs["v"] == 1


def test_cache_delete(session):
    session.python("cache['gone'] = 1")
    session.python("del cache['gone']")
    r = session.python("hit = 'gone' in cache")
    assert r.outputs["hit"] is False


def test_cache_missing_key_raises(session):
    r = session.python("try:\n    cache['nope']\nexcept KeyError:\n    caught = True")
    assert r.outputs["caught"] is True


class FakeDb:
    def __init__(self):
        self.rows = [{"id": 1, "name": "ada"}]
        self.log = []

    def query(self, filter=None):
        self.log.append(("query", filter))
        return self.rows

    def drop_all(self):  # pragma: no cover — must never be reachable
        raise AssertionError("should be blocked by allowlist")

    def _secret(self):  # pragma: no cover
        raise AssertionError("private must never be callable")


def test_hostcall_roundtrip():
    db = FakeDb()
    with Session(host_objects={"db": db}, allow={"db": {"query"}}) as s:
        r = s.python("rows = db.query(filter='x')\nn = len(rows)")
        assert r.ok, r.error
        assert r.outputs["n"] == 1 and r.outputs["rows"] == [{"id": 1, "name": "ada"}]
        assert db.log == [("query", "x")]


def test_hostcall_denied_method():
    db = FakeDb()
    with Session(host_objects={"db": db}, allow={"db": {"query"}}) as s:
        r = s.python("db.drop_all()")
        assert not r.ok and "not allowlisted" in r.error.message


def test_hostcall_private_always_denied():
    db = FakeDb()
    with Session(host_objects={"db": db}) as s:
        r = s.python("getattr(db, '_secret')()")
        assert not r.ok


def test_hostcall_unknown_object(session):
    r = session.python("nope.anything()")
    assert not r.ok and r.error.etype == "NameError"


def test_emit(session):
    r = session.python("emit('status', {'pct': 50})\nemit('done', True)")
    assert r.ok
    assert session.emits == [("status", {"pct": 50}), ("done", True)]


def test_emit_rejects_unrepresentable(session):
    r = session.python("emit('bad', object())")
    assert not r.ok and r.error.etype == "NotRepresentable"


@pytest.mark.parametrize("value", [42, "text", [1, 2], {"k": "v"}, None])
def test_hostcall_arg_types(value):
    class EchoObj:
        def echo(self, v):
            return v

    with Session(host_objects={"echo": EchoObj()}) as s:
        r = s.python("out = echo.echo(v)", inputs={"v": value})
        assert r.ok, r.error
        assert r.outputs.get("out") == value
