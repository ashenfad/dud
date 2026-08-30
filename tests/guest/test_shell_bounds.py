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


def test_pump_returns_when_the_survivor_never_stops_writing():
    """The chatty variant of the case above, and a separate code path.

    A survivor that writes with no gap keeps select() readable forever,
    so an exit check that only runs on a pass which read nothing never
    runs at all. The first cut of `_pump` had exactly that shape: it
    passed against a silent `sleep 30 &` and still timed out against a
    dev server logging as it boots — the example the function was
    written for. The transcript cap does not save it either; the reads
    keep succeeding, they just stop being stored.
    """
    proc = _spawn("(while :; do echo daemon-log; done) & echo script-done")
    try:
        started = time.monotonic()
        out, timed_out = _pump(proc, timeout=10.0)
        elapsed = time.monotonic() - started
        assert not timed_out, "a chatty survivor held the call to its deadline"
        assert "script-done" in out.text()
        assert elapsed < 5.0
    finally:
        _killpg(proc)  # the busy loop is in the group; do not leave it spinning
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


def test_timeout_is_not_held_open_by_a_silent_survivor():
    """The reported wedge. A process that escaped the group kill holds
    the write end open, and the old drain was an unbounded
    `communicate()` — which waits for EOF, so one silent `setsid`
    background process stalled the supervisor (single-threaded, PID 1)
    for as long as it lived. Silence is what made it unrecoverable:
    there was nothing to read and nothing to end the read.
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


def test_drain_gives_up_on_an_fd_that_is_always_readable():
    """`_drain`'s deadline, pinned with a synthetic rather than a process.

    Worth saying why it is synthetic. The test above — a silent
    survivor — passes with or without the deadline, because the pipe
    going quiet is what ends that drain. So it is not evidence for the
    budget, and a real *chatty* survivor is not either: `_drain` polls
    with a zero timeout, so after each read the writer needs microseconds
    to refill and loses the race every time. Measured while trying to
    write that test — the pipe reported quiet after a single 64 KB read.

    Which leaves the deadline reachable only by an fd that is readable
    the instant it is asked, forever. `/dev/zero` is exactly that, and
    it is what defense-in-depth against a faster writer would look like
    from the inside. The budget is belt-and-braces over the zero-timeout
    poll; this pins that the braces exist.
    """
    fd = os.open("/dev/zero", os.O_RDONLY)
    try:
        t = _Transcript()
        started = time.monotonic()
        _drain(t, fd, budget=0.3)
        elapsed = time.monotonic() - started
        assert 0.2 < elapsed < 3.0, f"drain ignored its budget ({elapsed:.2f}s)"
    finally:
        os.close(fd)


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


# ---- the emit reader ----------------------------------------------------


def _dispatch(*lines: bytes):
    """Feed raw bytes through _Emits as if they arrived on the pipe."""
    from dud.guest.shell import _Emits

    got = []
    e = _Emits(got.append)
    r, w = os.pipe()
    try:
        for chunk in lines:
            os.write(w, chunk)
        os.close(w)
        while e.read(r):
            pass
    finally:
        os.close(r)
    return got, e


def test_emits_are_dispatched_per_line():
    got, e = _dispatch(b'{"name":"a","value":{"t":"json","v":1}}\n'
                       b'{"name":"b","value":{"t":"json","v":2}}\n')
    assert [x["name"] for x in got] == ["a", "b"]
    assert e.dropped == 0


def test_a_record_split_across_reads_is_reassembled():
    """Pipe reads land where the kernel decides, not on record
    boundaries — so a frame arriving in two chunks must not become two
    broken ones."""
    got, _ = _dispatch(b'{"name":"a","val', b'ue":{"t":"json","v":1}}\n')
    assert [x["name"] for x in got] == ["a"]


def test_a_malformed_line_does_not_take_the_good_ones_with_it():
    got, e = _dispatch(b'garbage\n'
                       b'{"name":"ok","value":{"t":"json","v":1}}\n')
    assert [x["name"] for x in got] == ["ok"]
    assert e.dropped == 1


def test_a_well_formed_line_of_the_wrong_shape_is_refused():
    """Only `dud-emit` should be writing there. A stray write must not
    be able to put an arbitrary body on the host's wire."""
    got, e = _dispatch(b'{"name":"a","value":"not-a-tagged-value"}\n',
                       b'{"verb":"shutdown"}\n',
                       b'[1,2,3]\n')
    assert got == [] and e.dropped == 3


def test_an_unterminated_flood_does_not_grow_forever():
    """No newline means no record, so the buffer would otherwise be an
    unbounded write into a supervisor that is PID 1 on a VM rung."""
    from dud.guest.shell import _Emits

    e = _Emits(lambda rec: None)
    r, w = os.pipe()
    try:
        big = b"z" * (1 << 16)
        for _ in range((e._CAP // len(big)) + 4):
            os.write(w, big)
            e.read(r)
        assert len(e._buf) <= e._CAP
        assert e.dropped >= 1
    finally:
        os.close(w)
        os.close(r)


def test_a_failing_relay_is_not_the_scripts_fault():
    """The host round trip can fail on its own; that must not take down
    an exec whose only involvement was printing a line."""
    from dud.guest.shell import _Emits

    def boom(record):
        raise RuntimeError("upstream is unhappy")

    e = _Emits(boom)
    r, w = os.pipe()
    try:
        os.write(w, b'{"name":"a","value":{"t":"json","v":1}}\n')
        os.close(w)
        e.read(r)
    finally:
        os.close(r)
    assert e.dropped == 1 and e.dispatched == 0


def test_emit_drain_takes_what_is_left_in_the_pipe():
    """The drain after the script ends, pinned as a mechanism.

    Whether it is *needed* on any given run is a race — the select loop
    usually reads an emit before it notices the exit — so an integration
    test for it would pass with the drain removed most of the time.
    What is deterministic is that a record already in the pipe when the
    script is gone must still be collected, which is this.
    """
    from dud.guest.shell import _Emits

    got = []
    e = _Emits(got.append)
    r, w = os.pipe()
    try:
        os.write(w, b'{"name":"last","value":{"t":"json","v":1}}\n')
        e.drain(r, budget=0.5)  # nothing has read from the loop yet
    finally:
        os.close(w)
        os.close(r)
    assert [x["name"] for x in got] == ["last"]
