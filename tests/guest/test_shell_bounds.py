"""The shell exec's resource bounds.

Host-side unit tests over `dud.guest.shell`. The conformance corpus
pins the behavior on every rung; these pin the mechanics that produce
it, including two failure modes that need a real process to exhibit
and would be invisible to a mock.
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from dud.guest.runner import _CAP_STDOUT
from dud.guest.shell import (
    _CAP_TRANSCRIPT,
    ShellState,
    _drain,
    _killpg,
    _pump,
    _Transcript,
    run_shell,
)


def _spawn(script: str) -> subprocess.Popen:
    return subprocess.Popen(
        ["bash", "--noprofile", "--norc", "-c", script],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=True,
    )


# ---- the buffer ---------------------------------------------------------


def test_transcript_keeps_the_head_and_counts_the_rest():
    t = _Transcript(cap=10)
    r, w = os.pipe()
    os.write(w, b"0123456789ABCDEFG")
    os.close(w)
    assert t.read(r) is True
    assert t.read(r) is False  # EOF
    os.close(r)
    text = t.text()
    assert text.startswith("0123456789")
    assert "ABCDEFG" not in text  # the tail is dropped, not kept
    assert t.dropped == 7
    assert "7 more not captured" in text


def test_transcript_says_nothing_when_it_dropped_nothing():
    t = _Transcript(cap=64)
    r, w = os.pipe()
    os.write(w, b"hello\n")
    os.close(w)
    t.read(r)
    os.close(r)
    assert t.text() == "hello\n"  # no notice bolted onto an intact one


def test_transcript_cap_is_the_python_runners_number():
    """Parity is the entire point of the bound, so it is structural.

    The bug this closes was the two paths disagreeing by 200x on the
    same field of the same result object; two independently-maintained
    constants would let them drift apart again quietly.
    """
    assert _CAP_TRANSCRIPT is _CAP_STDOUT


# ---- the kill -----------------------------------------------------------


def test_killpg_survives_a_group_led_by_a_zombie():
    """Darwin raises EPERM, not ESRCH, when the group's only member is
    an unreaped corpse — which is the ordinary state at a timeout whose
    script has already exited. Uncaught it left `run_shell` and reached
    the host as a PermissionError from exec_shell instead of a timeout.

    Deliberately never calls poll() before the kill: polling reaps the
    child, which is precisely what makes the bug disappear.
    """
    proc = _spawn("exit 0")
    time.sleep(0.5)  # exited, unreaped, still the group leader
    _killpg(proc)  # must not raise, on either errno
    proc.wait(timeout=5)


# ---- the pump -----------------------------------------------------------


def test_pump_returns_when_the_script_exits_though_output_is_held():
    """`nohup server &`: the script is done, but something it started
    inherited stdout and keeps the pipe open. Waiting for EOF there is
    waiting for the daemon, which turned a successful one-second script
    into a full-timeout `timed_out=True`.
    """
    proc = _spawn("sleep 30 & echo started")
    try:
        started = time.monotonic()
        out, timed_out = _pump(proc, timeout=20.0)
        elapsed = time.monotonic() - started
        assert not timed_out
        assert "started" in out.text()
        assert elapsed < 10.0, "waited on the survivor's fd, not the script"
    finally:
        _killpg(proc)
        proc.stdout.close()
        proc.wait(timeout=5)


def test_pump_waits_for_a_script_that_closed_its_own_stdout():
    """The mirror case: end-of-pipe is not the script being finished
    either. `exec 1>&-` must not be read as an early exit."""
    proc = _spawn("echo pre; exec 1>&-; sleep 0.5; exit 5")
    try:
        out, timed_out = _pump(proc, timeout=20.0)
        assert not timed_out
        assert out.text() == "pre\n"
        assert proc.returncode == 5  # we waited for the real answer
    finally:
        proc.stdout.close()


def test_pump_bounds_a_timeout_that_a_survivor_would_otherwise_hold():
    """The wedge. A process that escaped the group kill holds the write
    end, and the drain after the kill used to be an unbounded
    `communicate()` — so one `setsid` background process stalled the
    supervisor (single-threaded, PID 1) for as long as it lived.
    """
    proc = _spawn(
        "python3 -c 'import os,time; os.setsid(); time.sleep(30)' & sleep 30"
    )
    try:
        started = time.monotonic()
        out, timed_out = _pump(proc, timeout=1.0)
        elapsed = time.monotonic() - started
        assert timed_out
        # 1s deadline + a 0.5s drain budget, with room for a loaded runner.
        assert elapsed < 6.0, f"drain was not bounded ({elapsed:.1f}s)"
    finally:
        _killpg(proc)
        proc.stdout.close()
        proc.wait(timeout=5)
        subprocess.run(["pkill", "-f", "time.sleep(30)"], check=False)


def test_drain_returns_promptly_when_nothing_is_coming():
    r, w = os.pipe()  # held open, never written: the survivor's fd
    try:
        t = _Transcript()
        started = time.monotonic()
        _drain(t, r, budget=0.3)
        assert time.monotonic() - started < 2.0
        assert t.text() == ""
    finally:
        os.close(r)
        os.close(w)


# ---- run_shell as a whole ----------------------------------------------


def test_timeout_does_not_replay_a_half_written_session(tmp_path):
    """SIGKILL means the EXIT trap never completed — and it can be shot
    between its two writes, leaving a cwd file with no env file. A
    timeout restores nothing rather than calling that pair a state."""
    work = tmp_path / "work"
    (work / "sub").mkdir(parents=True)
    state = ShellState(cwd=str(work), env={"PATH": os.environ["PATH"]})
    out = run_shell(state, "cd sub && export Q=set && sleep 30",
                    timeout=1.0, workspace=str(work))
    assert out.timed_out and out.exit_code == 124
    assert state.cwd == str(work)  # not the cd
    assert "Q" not in state.env  # not the export


@pytest.mark.parametrize("script,expect", [("echo hi", "hi\n"), ("exit 3", "")])
def test_ordinary_scripts_are_untouched(tmp_path, script, expect):
    state = ShellState(cwd=str(tmp_path), env={"PATH": os.environ["PATH"]})
    out = run_shell(state, script, timeout=10.0, workspace=str(tmp_path))
    assert out.transcript == expect and not out.timed_out
