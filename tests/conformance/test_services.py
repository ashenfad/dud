"""Conformance: cache / hostcall / emit — the reverse channel."""

import pickle

import pytest



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


def test_a_moderate_stash_is_the_point_of_the_cache(session):
    """The ceiling is set so ordinary stashing never meets it. Pinned
    because a guard aimed at the wrong number would break the feature
    it is protecting — the cache exists to carry working data between
    execs, not to carry an observation."""
    r = session.python("cache['data'] = 'z' * 20_000_000")
    assert r.ok, r.error
    r2 = session.python("n = len(cache['data'])")
    assert r2.outputs["n"] == 20_000_000


def test_an_oversized_stash_fails_the_exec_and_keeps_its_output(session):
    """Failed, not silently dropped: `cache[k] = v` is something the
    agent asked for by name, and a stash that quietly did not happen
    surfaces next session as an unexplained miss.

    The transcript survives, because an exec whose only fault was the
    size of its last statement should not also lose the evidence of
    what it did."""
    r = session.python(
        "print('work happened')\ncache['big'] = 'z' * 4_000_000",
        caps={"cache": 1_000_000},
    )
    assert not r.ok
    assert r.error.etype == "ValueTooLarge"
    assert "cache['big']" in r.error.message
    assert r.transcript.strip() == "work happened"
    r2 = session.python("hit = 'big' in cache")
    assert r2.outputs["hit"] is False  # nothing was applied


def test_the_stash_ceiling_is_the_callers_to_raise(session):
    r = session.python(
        "cache['big'] = 'z' * 4_000_000",
        caps={"cache": 8_000_000, "cache_total": 8_000_000},
    )
    assert r.ok, r.error
    r2 = session.python("n = len(cache['big'])")
    assert r2.outputs["n"] == 4_000_000


def test_cache_delete(session):
    session.python("cache['gone'] = 1")
    session.python("del cache['gone']")
    r = session.python("hit = 'gone' in cache")
    assert r.outputs["hit"] is False


def test_cache_read_is_not_a_write(make_session):
    """Merely reading a key must not ship it back as a cache write."""
    class CountingCache(dict):
        def __init__(self):
            super().__init__()
            self.sets = 0

        def __setitem__(self, key, value):
            self.sets += 1
            super().__setitem__(key, value)

    cache = CountingCache()
    with make_session(cache=cache) as s:
        s.python("cache['seed'] = [1, 2, 3]")
        writes_after_seed = cache.sets
        s.python("total = sum(cache['seed'])")
        assert cache.sets == writes_after_seed  # read-only exec: no churn
        s.python("cache['seed'].append(4)")  # in-place mutation still lands
        r = s.python("four = cache['seed'][-1]")
        assert r.outputs["four"] == 4
        assert cache.sets > writes_after_seed


def test_cache_carries_arbitrary_bytes_intact(session):
    """Values ride binary frames rather than base64 in the JSON body,
    matched to their keys by order. Bytes that JSON could never hold
    are the sharp end of that: a framing bug shows up here as
    corruption or a mismatched key, not as a slow path."""
    session.python(
        "cache['p'] = bytes(range(256))\n"
        "cache['q'] = {'k': [1, 2]}\n"
        "cache['r'] = 'text'"
    )
    assert sorted(session.cache) == ["p", "q", "r"]
    r = session.python("a = cache['p']\nb = cache['q']\nc = cache['r']")
    assert r.ok, r.error
    assert r.outputs["a"] == bytes(range(256))
    assert r.outputs["b"] == {"k": [1, 2]} and r.outputs["c"] == "text"


def test_cache_survives_a_multi_megabyte_value(session):
    """The size where base64 hurt: a 1.33x string encoded here, parsed
    by the supervisor forwarding it, and decoded again. Pinned for
    correctness at scale, not for speed."""
    n = 2 << 20
    r = session.python(f"cache['blob'] = b'z' * {n}")
    assert r.ok, r.error
    r2 = session.python("size = len(cache['blob'])\nhead = cache['blob'][:4]")
    assert r2.ok, r2.error
    assert r2.outputs["size"] == n
    assert r2.outputs["head"] == b"zzzz"


def test_cache_readonly_blocks_writes(make_session):
    with make_session() as s:
        s.python("cache['seed'] = 1")
        r = s.python(
            "read = cache['seed']\n"
            "try:\n"
            "    cache['x'] = 2\n"
            "    wrote = True\n"
            "except PermissionError:\n"
            "    wrote = False\n",
            cache_readonly=True,
        )
        assert r.ok, r.error
        assert r.outputs["read"] == 1  # reads still work
        assert r.outputs["wrote"] is False  # writes raise
        assert "x" not in s.cache  # nothing leaked to the host


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


def test_hostcall_roundtrip(make_session):
    db = FakeDb()
    with make_session(host_objects={"db": db}, allow={"db": {"query"}}) as s:
        r = s.python("rows = db.query(filter='x')\nn = len(rows)")
        assert r.ok, r.error
        assert r.outputs["n"] == 1 and r.outputs["rows"] == [{"id": 1, "name": "ada"}]
        assert db.log == [("query", "x")]


def test_hostcall_denied_method(make_session):
    db = FakeDb()
    with make_session(host_objects={"db": db}, allow={"db": {"query"}}) as s:
        r = s.python("db.drop_all()")
        assert not r.ok and "not allowlisted" in r.error.message


def test_hostcall_private_always_denied(make_session):
    db = FakeDb()
    # Underscore names are refused even when the allowlist names them:
    # the private rule outranks the policy, it isn't an absence of one.
    with make_session(host_objects={"db": db}, allow={"db": {"_secret"}}) as s:
        r = s.python("getattr(db, '_secret')()")
        assert not r.ok


def test_registering_without_an_allowlist_is_refused(make_session):
    """The default that used to hand a guest every public method.

    Fail closed at construction, like a rung the host can't provide —
    an unspecified policy is a question, not a permission.
    """
    import pytest

    from dud.errors import PolicyError

    with pytest.raises(PolicyError, match="without an allow entry"):
        make_session(host_objects={"db": FakeDb()})


def test_public_methods_grants_the_whole_object(make_session):
    """The honest way to say "expose all of it" — a resolved set, not a
    wildcard, so the gate stays a plain membership test and the grant
    stays inspectable. The private rule still outranks it."""
    import dud

    db = FakeDb()
    with make_session(host_objects={"db": db},
                      allow={"db": dud.public_methods(db)}) as s:
        assert s.python("rows = db.query()").ok
        r = s.python("getattr(db, '_secret')()")
        assert not r.ok  # underscore names are never callable


def test_empty_allowlist_registers_an_object_with_no_methods(make_session):
    """Explicitly nothing is a legitimate policy; only silence is not."""
    db = FakeDb()
    with make_session(host_objects={"db": db}, allow={"db": set()}) as s:
        r = s.python("db.query()")
        assert not r.ok and "not allowlisted" in r.error.message
        assert db.log == []


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
def test_hostcall_arg_types(value, make_session):
    class EchoObj:
        def echo(self, v):
            return v

    with make_session(host_objects={"echo": EchoObj()},
                      allow={"echo": {"echo"}}) as s:
        r = s.python("out = echo.echo(v)", inputs={"v": value})
        assert r.ok, r.error
        assert r.outputs.get("out") == value
