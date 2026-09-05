"""`dud-hostcall`: the synchronous shell→host path.

Host-side unit tests over `dud.guest.hostcall` and `dud.guest.shell`.
Like the emit CLI tests these pin the pieces (framing, printing,
failure modes); the conformance corpus proves the channel on every
rung. Every test that needs a caller uses a real bash script through
`run_shell` with a stub upstream — including the failure modes most
likely to wedge an exec, which would be invisible to a mock.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time

import pytest

from dud.guest import hostcall
from dud.guest.shell import (
    _DROP_VARS,
    _FRAME_CAP,
    _Hostcalls,
    ShellState,
    run_shell,
)


def _env(extra=None):
    import dud.guest.hostcall as _hc

    env = {
        "PATH": os.environ["PATH"],
        "PY": sys.executable,
        # By file path, not -m: the script's cwd is a tmp dir where
        # `dud` is not importable, and this module is stdlib-only.
        "HC": _hc.__file__,
    }
    if extra:
        env.update(extra)
    return env


def _state(tmp_path, **kw):
    return ShellState(cwd=str(tmp_path), env=_env(kw))


def _run(tmp_path, script, on_hostcall, timeout=10.0):
    return run_shell(
        _state(tmp_path), script, timeout=timeout,
        workspace=str(tmp_path), on_hostcall=on_hostcall,
    )


def _ok(value):
    return {"ok": True, "value": value}


# ---- CLI argument handling ------------------------------------------------


def test_needs_obj_and_method(capsys):
    assert hostcall.main([]) == 2
    assert hostcall.main(["onlyobj"]) == 2
    assert hostcall.main(["-x", "query"]) == 2
    assert "usage: dud-hostcall" in capsys.readouterr().err


def test_outside_an_exec_says_so_plainly(capsys, monkeypatch):
    for var in (hostcall.REQ_VAR, hostcall.RESP_VAR, hostcall.LOCK_VAR):
        monkeypatch.delenv(var, raising=False)
    assert hostcall.main(["db", "query"]) == 1
    err = capsys.readouterr().err
    assert hostcall.REQ_VAR in err and "inside a dud shell exec" in err


def test_oversized_request_is_refused(capsys, tmp_path, monkeypatch):
    r1, w1 = os.pipe()
    r2, w2 = os.pipe()
    lock = tmp_path / "hc.lock"
    lock.touch()
    monkeypatch.setenv(hostcall.REQ_VAR, str(w1))
    monkeypatch.setenv(hostcall.RESP_VAR, str(r2))
    monkeypatch.setenv(hostcall.LOCK_VAR, str(lock))
    try:
        assert hostcall.main(["db", "query", "z" * (hostcall.CAP + 1)]) == 1
        assert "over the" in capsys.readouterr().err
    finally:
        for fd in (r1, w1, r2, w2):
            try:
                os.close(fd)
            except OSError:
                pass


# ---- value printing -------------------------------------------------------


def test_prints_strings_raw_other_json_compact(capsys):
    hostcall._print_value({"t": "json", "v": "hi\n"})
    hostcall._print_value({"t": "json", "v": {"n": 3}})
    out = capsys.readouterr().out
    assert out == 'hi\n{"n":3}\n'


def test_prints_bytes_raw(capsysbinary):
    hostcall._print_value({"t": "bytes", "b64": base64.b64encode(b"a\x00b").decode()})
    out, _ = capsysbinary.readouterr()
    assert out == b"a\x00b"


def test_untagged_value_is_a_host_bug(capsys):
    with pytest.raises(ValueError, match="untagged"):
        hostcall._print_value({"v": 1})


# ---- _Hostcalls framing ----------------------------------------------------


def _hostcalls_pair(on_hostcall):
    """A _Hostcalls wired to bare pipes; returns (hc, req_w, resp_r)."""
    req_r, req_w = os.pipe()
    resp_r, resp_w = os.pipe()
    return _Hostcalls(on_hostcall, resp_w), req_r, req_w, resp_r, resp_w


def _close(*fds):
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


def _request(obj="db", method="query", args=None):
    return (
        json.dumps(
            {"obj": obj, "method": method,
             "args": args if args is not None else []},
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


def _read_frame(fd, buf):
    """Next response frame; ``buf`` carries over-read bytes between calls."""
    import select

    while b"\n" not in buf:
        # Bounded: a dropped answer must fail the test, not hang it.
        ready, _, _ = select.select([fd], [], [], 10)
        assert ready, "no answer frame arrived (dropped, not answered)"
        chunk = os.read(fd, 65536)
        if not chunk:
            raise AssertionError("answer channel closed without a frame")
        buf += chunk
    line, _, rest = bytes(buf).partition(b"\n")
    buf[:] = rest
    return json.loads(line)


def test_valid_frame_is_relayed_and_answered():
    got = []

    def relay(frame):
        got.append(frame)
        return _ok({"t": "json", "v": "yes"})

    hc, req_r, req_w, resp_r, resp_w = _hostcalls_pair(relay)
    try:
        os.write(req_w, _request(args=[{"t": "json", "v": "x"}]))
        assert hc.read(req_r, time.monotonic() + 5) is True
        assert got[0]["args"] == [{"t": "json", "v": "x"}]
        assert _read_frame(resp_r, bytearray()) == _ok({"t": "json", "v": "yes"})
        assert hc.dispatched == 1
    finally:
        _close(req_r, req_w, resp_r, resp_w)


def test_malformed_frame_earns_an_error_not_a_hang():
    hc, req_r, req_w, resp_r, resp_w = _hostcalls_pair(
        lambda frame: pytest.fail("must not relay garbage")
    )
    try:
        os.write(req_w, b"not json\n")
        os.write(req_w, b'{"obj": 1}\n')
        assert hc.read(req_r, time.monotonic() + 5) is True
        buf = bytearray()
        first = _read_frame(resp_r, buf)
        assert first["ok"] is False
        second = _read_frame(resp_r, buf)
        assert second["ok"] is False
    finally:
        _close(req_r, req_w, resp_r, resp_w)


def test_relay_failure_is_an_answer():
    def relay(frame):
        raise PermissionError("denied")

    hc, req_r, req_w, resp_r, resp_w = _hostcalls_pair(relay)
    try:
        os.write(req_w, _request())
        assert hc.read(req_r, time.monotonic() + 5) is True
        answer = _read_frame(resp_r, bytearray())
        assert answer == {"ok": False, "error": "denied"}
    finally:
        _close(req_r, req_w, resp_r, resp_w)


def test_oversized_answer_becomes_a_capped_error():
    big = "z" * (_FRAME_CAP + 1)

    def relay(frame):
        return _ok({"t": "json", "v": big})

    hc, req_r, req_w, resp_r, resp_w = _hostcalls_pair(relay)
    try:
        os.write(req_w, _request())
        assert hc.read(req_r, time.monotonic() + 5) is True
        answer = _read_frame(resp_r, bytearray())
        assert answer["ok"] is False and "frame cap" in answer["error"]
    finally:
        _close(req_r, req_w, resp_r, resp_w)


def test_channel_vars_do_not_survive_into_session_env():
    assert hostcall.REQ_VAR in _DROP_VARS
    assert hostcall.RESP_VAR in _DROP_VARS
    assert hostcall.LOCK_VAR in _DROP_VARS


# ---- end to end through real bash ------------------------------------------


def _cli():
    return '"$PY" "$HC"'


def test_round_trip_through_bash(tmp_path):
    calls = []

    def relay(frame):
        calls.append(frame)
        return _ok({"t": "json", "v": "answer:" + frame["method"]})

    out = _run(
        tmp_path,
        f'OUT=$({_cli()} db query "select 1" 42); echo "got:$OUT"',
        relay,
    )
    assert not out.timed_out and out.exit_code == 0
    assert "got:answer:query" in out.transcript
    # Verbatim strings: no dud-emit-style JSON coercion.
    assert calls[0]["obj"] == "db"
    assert calls[0]["args"] == [
        {"t": "json", "v": "select 1"},
        {"t": "json", "v": "42"},
    ]


def test_host_error_is_reported_not_wedged(tmp_path):
    def relay(frame):
        return {"ok": False, "error": "no host object 'db'"}

    out = _run(
        tmp_path,
        f'{_cli()} db query; echo "script-exit:$?"',
        relay,
    )
    assert not out.timed_out
    assert "no host object 'db'" in out.transcript
    assert "script-exit:1" in out.transcript


def test_relay_exception_never_hangs_the_exec(tmp_path):
    def relay(frame):
        raise RuntimeError("boom")

    out = _run(tmp_path, f"{_cli()} db query; echo done", relay, timeout=10.0)
    assert not out.timed_out
    assert "boom" in out.transcript and "done" in out.transcript


def test_concurrent_calls_serialize(tmp_path):
    def relay(frame):
        time.sleep(0.05)
        return _ok({"t": "json", "v": frame["args"][0]["v"]})

    out = _run(
        tmp_path,
        f"{_cli()} db echo first & {_cli()} db echo second & wait",
        relay,
    )
    assert not out.timed_out and out.exit_code == 0
    assert "first" in out.transcript and "second" in out.transcript


def test_script_exit_code_survives_a_hostcall(tmp_path):
    out = _run(
        tmp_path,
        f'{_cli()} db query >/dev/null; echo body; exit 7',
        lambda frame: _ok({"t": "json", "v": ""}),
    )
    assert not out.timed_out and out.exit_code == 7
    assert out.transcript == "body\n"


def test_channel_unoffered_says_so(tmp_path):
    state = _state(tmp_path)
    out = run_shell(
        state, '"$PY" "$HC" db query',
        timeout=10.0, workspace=str(tmp_path),
    )
    assert out.exit_code == 1
    assert hostcall.REQ_VAR in out.transcript


def test_slow_relay_does_not_spend_the_script_timeout(tmp_path):
    def relay(frame):
        time.sleep(1.0)
        return _ok({"t": "json", "v": "late"})

    out = _run(tmp_path, f'{_cli()} db query; echo done', relay, timeout=5.0)
    assert not out.timed_out
    assert "late" in out.transcript and "done" in out.transcript


def test_answer_survives_a_relay_longer_than_the_timeout(tmp_path):
    """The answer's write deadline must already exclude the relay: a
    relay that consumes the remaining script budget used to drop its
    own answer (exit 124) before the pump's extension landed."""

    def relay(frame):
        time.sleep(0.6)
        return _ok({"t": "json", "v": "late"})

    out = _run(tmp_path, f'{_cli()} db query; echo done', relay, timeout=0.3)
    assert not out.timed_out
    assert "late" in out.transcript and "done" in out.transcript


# ---- supervisor relay -------------------------------------------------------


def test_relay_maps_result_and_denial():
    from dud.guest.supervisor import Supervisor

    seen = []

    class Channel:
        def request(self, verb, body, bins=None):
            seen.append((verb, body))
            return {"result": {"t": "json", "v": "ok"}}, []

    sup = Supervisor.__new__(Supervisor)
    sup.channel = Channel()
    answer = sup._relay_hostcall(
        {"obj": "db", "method": "q", "args": [{"t": "json", "v": 1}]}
    )
    assert answer == {"ok": True, "value": {"t": "json", "v": "ok"}}
    verb, body = seen[0]
    assert verb == "hostcall" and body["kwargs"] == {}

    class Denying:
        def request(self, verb, body, bins=None):
            raise PermissionError("denied")

    sup.channel = Denying()
    assert sup._relay_hostcall({"obj": "db", "method": "q", "args": []}) == {
        "ok": False,
        "error": "denied",
    }


def test_relay_maps_none_result_to_null():
    from dud.guest.supervisor import Supervisor

    class Channel:
        def request(self, verb, body, bins=None):
            return {}, []

    sup = Supervisor.__new__(Supervisor)
    sup.channel = Channel()
    assert sup._relay_hostcall({"obj": "db", "method": "q", "args": []}) == {
        "ok": True,
        "value": {"t": "json", "v": None},
    }
