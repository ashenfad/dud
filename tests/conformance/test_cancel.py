"""A host cancels the exec in flight: the guest kills it as a timeout
would, and the call answers at once with what it had."""

from __future__ import annotations

import threading
import time


def _in_flight(session, call):
    """Start ``call`` on a thread and return once its request is on the
    wire, with a box the thread puts its result in."""
    box: dict = {}

    def run():
        box["result"] = call()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while session._ch.awaiting is None:
        assert time.monotonic() < deadline, "the request never went out"
        assert thread.is_alive(), "the call ended before it could be cancelled"
        time.sleep(0.01)
    return thread, box


def _cancelled(session, call):
    """The result of ``call``, cancelled as soon as it is in flight, and
    how long it took to answer once cancelled."""
    thread, box = _in_flight(session, call)
    started = time.monotonic()
    assert session.cancel()
    thread.join(timeout=20)
    assert not thread.is_alive(), "the cancelled call never answered"
    return box["result"], time.monotonic() - started


def test_a_cancel_stops_a_python_exec(session):
    r, took = _cancelled(
        session, lambda: session.python("import time\ntime.sleep(30)", timeout=60)
    )
    assert not r.ok and r.error.etype == "Cancelled"
    assert took < 10
    # and the session goes on serving
    after = session.python("x = 1")
    assert after.outputs.get("x") == 1, after


def test_a_cancel_stops_a_shell(session):
    r, took = _cancelled(session, lambda: session.shell("sleep 30", timeout=60))
    assert r.cancelled and not r.timed_out and r.exit_code == 130
    assert took < 10
    assert session.shell("echo ok").transcript.strip() == "ok"


class Stopper:
    """A host object that cancels the exec calling it: the cancel is on
    the wire before the call's answer is."""

    session = None

    def stop(self):
        self.session.cancel()
        return "stopping"


def test_a_cancel_during_a_hostcall_lands_when_it_returns(make_session):
    """The guest is relaying the call upstream when the cancel comes,
    so it reads the cancel there, and acts on it once the call is
    answered rather than letting the code run on."""
    stopper = Stopper()
    with make_session(host_objects={"h": stopper}, allow={"h": {"stop"}}) as s:
        stopper.session = s
        started = time.monotonic()
        r = s.python("h.stop()\nimport time\ntime.sleep(30)", timeout=60)
        assert not r.ok and r.error.etype == "Cancelled"
        assert time.monotonic() - started < 10


def test_a_cancel_with_nothing_in_flight_does_nothing(session):
    assert session.cancel() is False
    assert session.python("x = 2").outputs["x"] == 2
