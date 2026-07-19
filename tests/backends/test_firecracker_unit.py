"""Firecracker backend logic that needs no Linux/KVM (runs anywhere).

The live contract is covered by the conformance corpus inside the
nested-virt Lima VM; these pin the host-testable edges so regressions
surface on macOS/CI without a VM: fail-closed availability, binary
resolution, factory routing, and the API plane's unix-socket HTTP.
"""

from __future__ import annotations

import http.server
import socket
import threading

import pytest

import dud
from dud.backends import firecracker as fc
from dud.errors import IsolationUnavailable


def test_non_linux_fails_closed():
    import platform

    if platform.system() == "Linux":
        pytest.skip("this is the non-Linux fail-closed test")
    with pytest.raises(IsolationUnavailable, match="requires Linux"):
        fc.FirecrackerSession()


def test_missing_kvm_fails_closed(monkeypatch):
    monkeypatch.setattr(fc.platform, "system", lambda: "Linux")
    monkeypatch.setattr(fc.os, "access", lambda *_: False)
    with pytest.raises(IsolationUnavailable, match="/dev/kvm"):
        fc.FirecrackerSession()


def test_fc_bin_resolution(tmp_path, monkeypatch):
    exe = tmp_path / "firecracker"
    exe.write_bytes(b"#!/bin/sh\n")
    monkeypatch.setenv("DUD_FIRECRACKER", str(exe))
    assert fc._fc_bin() == str(exe)
    monkeypatch.delenv("DUD_FIRECRACKER")
    monkeypatch.setattr(fc.shutil, "which", lambda _: None)
    with pytest.raises(IsolationUnavailable, match="firecracker not found"):
        fc._fc_bin()


def test_factory_firecracker_pooled_routes_to_shared_pool(monkeypatch):
    """pooled=True acquires from the fc-typed shared pool (frozen
    parking); state without pooled stays a loud error."""
    from dud.backends import pool as poolmod

    acquired = []

    class FakePool:
        def acquire(self, state=None, **kw):
            acquired.append((state, kw))
            return "pooled-session"

    monkeypatch.setattr(poolmod, "shared_pool",
                        lambda cls: FakePool())
    assert dud.session("firecracker", pooled=True, state="c1") == "pooled-session"
    assert acquired == [("c1", {})]
    with pytest.raises(ValueError, match="park affinity"):
        dud.session("firecracker", state="c1")


def test_factory_vm_resolves_per_platform(monkeypatch):
    import platform

    seen = []
    monkeypatch.setattr(fc, "FirecrackerSession",
                        lambda **kw: seen.append(kw) or "fc-session")
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    assert dud.session("vm", image="x") == "fc-session"
    assert seen == [{"image": "x"}]


def test_unknown_backend_lists_firecracker():
    with pytest.raises(ValueError, match="firecracker"):
        dud.session("qemu")


def test_unix_http_connection_roundtrip():
    import tempfile

    # Short-path anchor: macOS caps sun_path at 104 chars and pytest
    # tmp_paths blow it — the same constraint the rundirs live under.
    td = tempfile.mkdtemp(dir="/tmp", prefix="dud-t-")
    path = str(td + "/api.sock")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_PUT(self):
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n)
            self.send_response(204 if body == b'{"ok": true}' else 400)
            self.end_headers()

        def log_message(self, *args):
            pass

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)

    def serve_one():
        conn, _ = srv.accept()
        Handler(conn, ("local", 0), None)

    t = threading.Thread(target=serve_one)
    t.start()
    c = fc._UnixHTTPConnection(path)
    c.request("PUT", "/machine-config", body='{"ok": true}',
              headers={"Content-Type": "application/json"})
    resp = c.getresponse()
    assert resp.status == 204
    c.close()
    t.join(timeout=5)
    srv.close()
    import shutil

    shutil.rmtree(td, ignore_errors=True)


def test_frozen_session_requests_fail_with_guidance():
    """shell()/python() on a frozen session must say 'thaw()', not
    surface a bad-file-descriptor OSError."""
    from dud.backends.base import HostSession, SessionLost

    s = HostSession.__new__(HostSession)
    s.frozen = True
    with pytest.raises(SessionLost, match="thaw"):
        s._request("exec_shell", {"script": "echo hi"})
