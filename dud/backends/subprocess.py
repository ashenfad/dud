"""Rung 1: the guest runtime as a host subprocess. No isolation.

The dev/CI/demo floor (DESIGN.md "Backend ladder"): same supervisor,
same wire protocol as the VM rungs, zero artifacts — and zero
containment. Agent code runs as the host user with open egress.
Own-agent-own-laptop posture only.

Everything above the transport (the guest-service handler, cache
write-backs, the push/exec/diff API) lives in :class:`HostSession`; this
file owns only the socketpair + child process and their teardown.
"""

from __future__ import annotations

import os
import socket as socketlib
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from ..proto import Channel
from .base import HostSession


class Session(HostSession):
    """One workspace session against a guest supervisor subprocess."""

    def __init__(
        self,
        root: str | Path | None = None,
        host_objects: dict[str, Any] | None = None,
        allow: dict[str, set[str]] | None = None,
        cache: dict[str, bytes] | None = None,
        on_emit: Callable[[str, Any], None] | None = None,
    ):
        super().__init__(host_objects, allow, cache, on_emit)
        self._tmp = None
        if root is None:
            self._tmp = tempfile.TemporaryDirectory(prefix="dud-")
            root = self._tmp.name
        self.root = Path(root)

        parent, child = socketlib.socketpair()
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "dud.guest.supervisor",
             str(child.fileno()), str(self.root)],
            pass_fds=(child.fileno(),),
            env=dict(os.environ),
        )
        child.close()
        try:
            self._ch = Channel(parent, handler=self._handle)
            self._ch.hello_recv()
        except Exception:
            # Ctor failure (e.g. proto version mismatch) must not leave
            # an orphaned supervisor child behind.
            self._proc.kill()
            self._proc.wait(timeout=5)
            parent.close()
            if self._tmp is not None:
                self._tmp.cleanup()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._ch.request("shutdown")
        except Exception:
            pass
        self._ch.close()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        if self._tmp is not None:
            self._tmp.cleanup()
