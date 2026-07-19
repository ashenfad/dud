"""Supervisor side of the view-worker control protocol (no VM).

The template end is faked with a raw socketpair (reusing the real
template's frame parser where a live peer is needed), so the failure
branches — wedged handshake, torn pid reply, breaker trips — are
exercised directly. On the host getpid() != 1, so _start_template is
a no-op: "dropped" states stay observable as _template is None.
"""

from __future__ import annotations

import os
import socket
import threading
from pathlib import Path

import pytest

from dud.guest import supervisor as sup_mod
from dud.guest import template as tpl_mod
from dud.proto import Channel
from dud.guest.supervisor import Supervisor


class _FakeProc:
    pid = 999999  # never polled dead in these tests

    def poll(self):
        return None


@pytest.fixture
def sup(tmp_path):
    a, b = socket.socketpair()
    root = tmp_path / "root"
    root.mkdir()
    s = Supervisor(Channel(a), root)
    yield s
    a.close()
    b.close()


def _arm(sup):
    ours, theirs = socket.socketpair()
    sup._template = (_FakeProc(), ours)
    sup._template_ready = True
    return theirs


def test_wedged_template_handshake_drops_and_falls_back(sup, monkeypatch):
    monkeypatch.setattr(sup_mod, "_CTL_TIMEOUT", 0.2)
    theirs = _arm(sup)  # peer never answers
    assert sup._fork_from_template("/", {}) is None
    assert sup._template is None  # dropped; spawn path takes over
    theirs.close()


def test_torn_pid_reply_drops_template(sup):
    theirs = _arm(sup)

    def peer():
        got = tpl_mod._recv_request(theirs)
        if got is not None:
            os.close(got[1])
        theirs.sendall(b"abc")  # 3 of 8 pid bytes
        theirs.close()

    t = threading.Thread(target=peer)
    t.start()
    assert sup._fork_from_template("/", {"K": "v"}) is None
    t.join()
    assert sup._template is None


def test_large_env_payload_crosses_ctl_intact(sup):
    """Pins the header-then-sendall fix: an env bigger than any socket
    buffer must arrive whole (a single sendmsg sent partially and
    wedged the template mid-frame)."""
    theirs = _arm(sup)
    got: dict = {}

    def peer():
        req = tpl_mod._recv_request(theirs)
        assert req is not None
        body, fd = req
        got.update(body)
        os.close(fd)
        theirs.sendall((424242).to_bytes(8, "big"))
        theirs.close()

    t = threading.Thread(target=peer)
    t.start()
    blob = "x" * 1_000_000
    res = sup._fork_from_template("/", {"BLOB": blob})
    t.join()
    assert res is not None
    parent, child = res
    assert child.pid == 424242
    assert got["env"]["BLOB"] == blob
    parent.close()


def test_failure_breaker_replaces_template_after_two_strikes(sup):
    theirs = _arm(sup)
    bad = {"ok": False, "error": {"etype": "Timeout", "message": "wedged"}}
    sup._note_worker_outcome(bad)
    assert sup._template is not None  # one strike: could be the exec's fault
    sup._note_worker_outcome(bad)
    assert sup._template is None  # two strikes: fork-hostile, replaced
    assert sup._worker_failures == 0
    theirs.close()


def test_success_resets_the_breaker(sup):
    theirs = _arm(sup)
    bad = {"ok": False, "error": {"etype": "RunnerCrash", "message": "x"}}
    good = {"ok": True}
    sup._note_worker_outcome(bad)
    sup._note_worker_outcome(good)
    sup._note_worker_outcome(bad)
    assert sup._template is not None  # never two consecutive
    theirs.close()


def test_user_error_is_not_a_worker_failure(sup):
    theirs = _arm(sup)
    user_err = {"ok": False, "error": {"etype": "ValueError", "message": "boom"}}
    sup._note_worker_outcome(user_err)
    sup._note_worker_outcome(user_err)
    assert sup._template is not None  # guest answered: the worker works
    theirs.close()
