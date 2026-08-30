"""`dud-emit` itself: argument handling, framing, and the pipe write.

The conformance corpus proves the channel works on every rung; this
pins the pieces, including the two failure modes a shell user is most
likely to hit — running it outside an exec, and a value that is not
JSON.
"""

from __future__ import annotations

import json
import os

import pytest

from dud.guest import emit


def _read(r: int) -> list[dict]:
    os.set_blocking(r, False)
    try:
        raw = os.read(r, 1 << 16)
    except BlockingIOError:
        return []
    return [json.loads(line) for line in raw.split(b"\n") if line.strip()]


@pytest.fixture
def pipe():
    r, w = os.pipe()
    yield r, w
    for fd in (r, w):
        try:
            os.close(fd)
        except OSError:
            pass


# ---- values -------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ('{"n": 3}', {"n": 3}),
    ("[1, 2]", [1, 2]),
    ("42", 42),
    ("true", True),
    ("null", None),
    ('"42"', "42"),      # quoted: the escape hatch back to a string
    ("running", "running"),
    ("", ""),            # empty is not valid JSON, so it is the empty string
    ("{unclosed", "{unclosed"),
])
def test_a_word_is_json_if_it_parses_else_a_string(raw, expected):
    assert emit.tag(raw) == {"t": "json", "v": expected}


def test_no_value_is_null_like_python():
    assert emit.tag(None) == {"t": "json", "v": None}


def test_a_record_is_one_newline_terminated_line():
    """NDJSON, and the delimiter is only unambiguous because the encoder
    escapes newlines inside strings. `dud-emit out "$(cat file)"` is the
    ordinary way to hit this from bash, and a value that split its own
    record would corrupt the stream for everything after it.

    Both routes in, because they take different paths: strict JSON
    rejects a literal newline inside a string, so a multi-line shell
    word falls through to the plain-string branch rather than parsing.
    """
    escaped = emit.record("n", '"a\\nb"')  # valid JSON, parses
    literal = emit.record("n", "a\nb")  # not JSON, kept as a string
    for frame in (escaped, literal):
        assert frame.endswith(b"\n") and frame.count(b"\n") == 1
        assert json.loads(frame)["value"]["v"] == "a\nb"


# ---- the write ----------------------------------------------------------


def test_main_writes_one_record(pipe, monkeypatch):
    r, w = pipe
    monkeypatch.setenv(emit.FD_VAR, str(w))
    assert emit.main(["status", '{"pct": 50}']) == 0
    assert _read(r) == [{"name": "status", "value": {"t": "json",
                                                     "v": {"pct": 50}}}]


def test_main_without_a_channel_says_so(pipe, monkeypatch, capsys):
    """Outside an exec_shell there is nothing to emit to. Silence would
    be the worst answer: an event that went nowhere is indistinguishable
    from one that was never fired."""
    monkeypatch.delenv(emit.FD_VAR, raising=False)
    assert emit.main(["status", "1"]) == 1
    assert emit.FD_VAR in capsys.readouterr().err


def test_main_rejects_a_bad_usage(capsys):
    assert emit.main([]) == 2
    assert emit.main(["a", "b", "c"]) == 2
    assert "usage" in capsys.readouterr().err


def test_main_refuses_an_oversized_record(pipe, monkeypatch, capsys):
    r, w = pipe
    monkeypatch.setenv(emit.FD_VAR, str(w))
    assert emit.main(["big", '"' + "z" * (emit.CAP + 10) + '"']) == 1
    assert "limit" in capsys.readouterr().err
    assert _read(r) == []  # nothing partial reached the pipe


def test_main_reports_a_dead_pipe_rather_than_crashing(monkeypatch, capsys):
    r, w = os.pipe()
    os.close(r)  # no reader: the write will EPIPE
    monkeypatch.setenv(emit.FD_VAR, str(w))
    try:
        assert emit.main(["x", "1"]) == 1
        assert "could not write" in capsys.readouterr().err
    finally:
        os.close(w)


def test_the_lock_is_optional(pipe, monkeypatch):
    """Locking is what makes concurrent writers safe, but a missing
    lock path must degrade to a plain write rather than failing."""
    r, w = pipe
    monkeypatch.setenv(emit.FD_VAR, str(w))
    monkeypatch.delenv(emit.LOCK_VAR, raising=False)
    assert emit.main(["x", "1"]) == 0
    assert _read(r)[0]["name"] == "x"


def test_writers_serialize_through_the_lock(pipe, tmp_path):
    """Concurrent writers produce whole records.

    Honest about what this does and does not show. POSIX guarantees
    pipe-write atomicity only up to PIPE_BUF — 512 bytes on macOS — so
    above that a concurrent write MAY interleave, which is why there is
    a lock rather than a cap at the platform's own PIPE_BUF (a cap
    would make the rungs differ, since Linux's is 4096).

    But "may" is not "does": removing the lock leaves this passing, and
    it survived every attempt to force tearing — real processes rather
    than threads, records at 60 KB, a reader draining concurrently so
    the writers block mid-record. So this is a positive check that the
    locked path delivers eight intact records, not evidence that the
    lock is load-bearing. The lock stays because the specification
    permits the tear, not because a test caught one.
    """
    import subprocess
    import threading

    r, w = pipe
    os.set_inheritable(w, True)
    lock = tmp_path / "emit.lock"
    lock.touch()
    env = {**os.environ, emit.FD_VAR: str(w), emit.LOCK_VAR: str(lock)}

    # Big enough, and enough of them, that the pipe buffer fills and
    # writers block partway through a record — which is when an
    # unlocked write actually tears. Eight small ones all fit at once
    # and complete without interleaving, so they prove nothing.
    seen = bytearray()

    def reader():
        while True:
            chunk = os.read(r, 65536)
            if not chunk:
                return
            seen.extend(chunk)

    pump = threading.Thread(target=reader, daemon=True)
    pump.start()

    big = '"' + "z" * 60_000 + '"'
    procs = [
        subprocess.Popen(["dud-emit", f"k{i}", big], env=env, pass_fds=(w,))
        for i in range(8)
    ]
    for p in procs:
        assert p.wait(timeout=60) == 0
    os.close(w)  # let the reader see EOF (the fixture re-closes harmlessly)
    pump.join(timeout=30)

    records = [json.loads(line) for line in seen.split(b"\n") if line.strip()]
    assert len(records) == 8, "a record was torn by a concurrent write"
    assert sorted(x["name"] for x in records) == [f"k{i}" for i in range(8)]
