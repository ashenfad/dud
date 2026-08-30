"""ping() surfaces the one performance property nothing else reports.

An image that shipped without baked bytecode behaves identically and is
merely slower — forever, on a read-only root. Nothing raises, no test
fails, and CI cannot notice because CI pins the interpreter that makes
baking happen. So the only way it becomes findable is by being reported
where a consumer already looks.
"""

from __future__ import annotations

import types

from dud.backends.base import HostSession


class _Session(HostSession):
    """Enough HostSession to answer ping without a guest."""

    def __init__(self, build=None):
        super().__init__(None, None, None, None, None, None)
        if build is not None:
            self.build = build
        self.sent = []

    def _request(self, verb, body=None, bins=None):
        self.sent.append((verb, body))
        return {"pong": True, "staging": "overlay"}, []

    def close(self):
        pass


def test_ping_reports_the_builds_bytecode_status():
    build = types.SimpleNamespace(bytecode="skipped: host python 3.13 != guest 3.12")
    got = _Session(build).ping()
    assert got["bytecode"] == "skipped: host python 3.13 != guest 3.12"
    assert got["pong"] is True  # the guest's own answer is not disturbed


def test_ping_omits_bytecode_on_a_rung_with_no_image():
    """Rung 1 runs the guest as a host process: there is no image, so
    reporting a bake status would be inventing one."""
    assert "bytecode" not in _Session().ping()
