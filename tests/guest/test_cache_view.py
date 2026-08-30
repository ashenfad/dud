"""The guest cache view, and the ceiling on what one exec may stash.

Cache writes are the one guest->host value path that rides binary
frames rather than the JSON body, so nothing in the codec's guards
ever saw them. Cheaper per byte — no parse, no base64 — but the bytes
still land whole in a supervisor that is PID 1 on a VM rung, so
"cheaper" is not "free".

The ceiling is deliberately far above the codec's: an output is an
observation, the cache is working storage, and stashing data between
execs is what it is for.
"""

from __future__ import annotations

import pickle

import pytest

from dud.guest.runner import CacheView
from dud.values import ValueTooLarge


class _NoChannel:
    def request(self, *a, **k):  # pragma: no cover - defensive
        raise AssertionError("these tests never read through")


def _view() -> CacheView:
    return CacheView(_NoChannel())


def test_writes_flush_as_pickles():
    c = _view()
    c["a"] = {"n": 1}
    writes, deletes = c.flush()
    assert pickle.loads(writes["a"]) == {"n": 1}
    assert deletes == []


def test_no_cap_is_the_old_behavior():
    c = _view()
    c["a"] = "z" * 100_000
    writes, _ = c.flush()
    assert len(writes["a"]) > 100_000


def test_an_oversized_write_names_its_key():
    """The message has to say WHICH stash was refused: an exec that
    caches several things gets one error, and "something was too big"
    sends the author reading all of them."""
    c = _view()
    c["small"] = 1
    c["fat"] = "z" * 100_000
    with pytest.raises(ValueTooLarge) as e:
        c.flush(cap=10_000)
    assert "cache['fat']" in str(e.value)
    assert "per-write limit" in str(e.value)


def test_a_total_bounds_several_legal_writes():
    """Individually fine, collectively not — the same arithmetic that
    let twenty legal hostcall arguments assemble into 140 MB."""
    c = _view()
    for i in range(4):
        c[f"k{i}"] = "z" * 10_000
    with pytest.raises(ValueTooLarge) as e:
        c.flush(cap=50_000, total=25_000)
    assert "cache writes total" in str(e.value)


def test_a_write_at_the_limit_still_goes():
    c = _view()
    c["a"] = "z" * 1_000
    writes, _ = c.flush(cap=1_000_000, total=1_000_000)
    assert "a" in writes


def test_the_size_measured_is_the_size_that_ships():
    """Sized at flush, not at assignment, because in-place mutation is
    a supported way to write: `cache['x'].append(...)` is documented to
    be captured, so the value at `__setitem__` is not the value that
    crosses. A guard on the assignment would miss exactly that case."""
    c = _view()
    c["grow"] = []
    c["grow"].extend(range(50_000))  # after the assignment
    with pytest.raises(ValueTooLarge):
        c.flush(cap=1_000)


def test_an_unchanged_read_is_not_charged():
    """Keys only read ship back only if they differ, so an untouched
    big value must not consume the exec's budget."""
    c = _view()
    raw = pickle.dumps("z" * 100_000, protocol=pickle.HIGHEST_PROTOCOL)
    c._local["big"] = "z" * 100_000
    c._fetched["big"] = raw
    c["small"] = 1
    writes, _ = c.flush(cap=10_000, total=10_000)
    assert list(writes) == ["small"]


def test_deletes_are_unaffected_by_the_ceiling():
    c = _view()
    c._local["gone"] = 1
    del c["gone"]
    writes, deletes = c.flush(cap=10, total=10)
    assert deletes == ["gone"] and writes == {}
