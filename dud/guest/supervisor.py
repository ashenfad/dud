"""The guest supervisor: one process per session, serving the wire verbs.

Owns the workspace staging (overlayfs on VM rungs, baseline-copy
scan-diff elsewhere — see :mod:`dud.guest.staging`), the shell session
state, and the runner lifecycle. On
``exec_python`` it spawns a fresh runner process (script model: one
interpreter per exec, killed on timeout) and pumps: runner requests
(cache/hostcall/emit) forward upstream to the host, whose own
``request()`` loop services them mid-exec; the run response comes back
as the exec_python response.

Identical on every rung — this file IS the guest contract. On the
subprocess rung it runs as a host child over a socketpair; on VM rungs
it runs as guest PID-adjacent over vsock.

Invoked as: python -m dud.guest.supervisor <socket-fd> <root-dir>
"""

from __future__ import annotations

import os
import select
import signal
import socket as socketlib
import subprocess
import sys
import time
from pathlib import Path

from contextlib import nullcontext

from ..proto import Channel, ChannelClosed, RemoteError, shutdown_served
from .shell import ShellOutcome, ShellState, run_shell
from .staging import make_stage

_RUNNER_DEFAULT_TIMEOUT = 30.0
_SHELL_DEFAULT_TIMEOUT = 30.0


class Supervisor:
    def __init__(self, channel: Channel, root: Path):
        self.channel = channel
        self.root = root
        # Staging strategy (overlay on VM rungs, scan-diff otherwise)
        # owns the trees; the supervisor only knows the mutable work
        # dir and the wire verbs.
        self.stage = make_stage(root)
        self.work = self.stage.work
        self.shell = ShellState(cwd=str(self.work))
        # Boot-time env snapshot: reset_guest restores this, so exports
        # from one pooled session never leak into the next.
        self._boot_env = dict(os.environ)
        channel.handler = self.handle

    # ---- dispatch ----------------------------------------------------

    def handle(self, verb: str, body: dict, bins: list[bytes]):
        method = getattr(self, f"do_{verb.replace('.', '_')}", None)
        if method is None:
            raise ValueError(f"unknown verb {verb!r}")
        return method(body, bins)

    # ---- verbs -------------------------------------------------------

    def do_ping(self, body, bins):
        return {"pong": True, "workspace": str(self.work),
                "staging": self.stage.kind}, []

    def do_shutdown(self, body, bins):
        shutdown_served()

    def do_push_tree(self, body, bins):
        self.stage.push(bins[0] if bins and bins[0] else None)
        self.shell.cwd = str(self.work)
        return {}, []

    def do_exec_shell(self, body, bins):
        outcome: ShellOutcome = run_shell(
            self.shell,
            body["script"],
            float(body.get("timeout", _SHELL_DEFAULT_TIMEOUT)),
            workspace=str(self.work),
        )
        return {
            "transcript": outcome.transcript,
            "exit_code": outcome.exit_code,
            "timed_out": outcome.timed_out,
            "cwd": self.shell.cwd,
        }, []

    def do_pull_diff(self, body, bins):
        writes, deletes, tar = self.stage.diff(bool(body.get("rebase")))
        return {"writes": writes, "deletes": deletes}, [tar]

    def do_reset_stage(self, body, bins):
        self.stage.reset_stage()
        return {}, []

    def do_reset_guest(self, body, bins):
        """Session-hygiene reset for VM reuse (pooling): wipe both trees,
        restore the boot-time shell state, and — on the VM rung, where the
        supervisor is PID 1 — kill every other process, so one session's
        exports, files, and stray daemons never reach the next. NOT the
        same as reset_stage (a rollback within one session).

        ``keep_tree`` parks the workspace in place (state-affinity
        pooling: the tree is tagged with its provider commit and a
        matching session resumes without a push). Env and process
        hygiene still apply; a mismatched consumer is safe regardless,
        because push_tree wipes before extracting."""
        if os.getpid() == 1:  # VM rung only: we own the machine
            # Kill strays BEFORE touching the trees — a leftover daemon
            # with its cwd inside the workspace would pin the overlay
            # mount we're about to cycle. SIGKILL is async, so wait for
            # the machine to actually be ours again: reap everything
            # that reparented to us (we're PID 1 — unreaped strays
            # would accumulate as zombies for the life of a pooled VM)
            # and re-scan /proc, bounded, until only PID 1 remains.
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                others = [e for e in os.listdir("/proc")
                          if e.isdigit() and e != "1"]
                for entry in others:
                    try:
                        os.kill(int(entry), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                try:
                    while os.waitpid(-1, os.WNOHANG) != (0, 0):
                        pass
                except ChildProcessError:
                    pass
                if not others:
                    break
                time.sleep(0.02)
        self.stage.reset_guest(bool(body.get("keep_tree")))
        self.shell = ShellState(cwd=str(self.work), env=dict(self._boot_env))
        # Flush block-backed state (the scratch volume) so the host's
        # park-time promotion copies a consistent image.
        if os.getpid() == 1:
            os.sync()
        return {}, []

    def do_exec_python(self, body, bins):
        timeout = float(body.get("timeout", _RUNNER_DEFAULT_TIMEOUT))
        run_body = {k: v for k, v in body.items() if k != "timeout"}

        # fs_readonly (view execs): on overlay staging this is a REAL
        # read-only remount for the exec's duration; scan staging can't
        # enforce it (rung-1 documented gap — consumers keep their
        # post-hoc diff check there).
        guard = (self.stage.readonly() if body.get("fs_readonly")
                 else nullcontext())
        parent, child = socketlib.socketpair()
        env = dict(self.shell.env)
        env["DUD_WORKSPACE"] = str(self.work)
        cwd = self.shell.cwd if os.path.isdir(self.shell.cwd) else str(self.work)
        with guard:
            proc = subprocess.Popen(
                [sys.executable, "-m", "dud.guest.runner", str(child.fileno())],
                pass_fds=(child.fileno(),),
                cwd=cwd,
                env=env,
                start_new_session=True,
            )
            child.close()
            rchan = Channel(parent)
            rchan._next_id += 1
            rid = rchan._next_id
            rchan._send_msg(
                {"id": rid, "kind": "req", "verb": "run", "body": run_body}, []
            )
            try:
                result = self._pump_runner(rchan, rid, proc, timeout)
            finally:
                rchan.close()
                self._reap(proc)
        return result, []

    # ---- runner pump -------------------------------------------------

    def _pump_runner(self, rchan: Channel, rid: int, proc, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        sock = rchan._sock
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._kill(proc)
                return {
                    "ok": False, "transcript": "", "prints": [],
                    "outputs": {}, "outputs_skipped": {},
                    "error": {"etype": "Timeout",
                              "message": f"exceeded {timeout}s (killed)"},
                }
            ready, _, _ = select.select([sock], [], [], min(remaining, 0.25))
            if not ready:
                if proc.poll() is not None:
                    return self._crash_result(proc)
                continue
            try:
                msg, mbins = rchan._recv_msg()
            except ChannelClosed:
                return self._crash_result(proc)
            kind = msg.get("kind")
            if kind == "req":
                try:
                    rbody, rbins = self.channel.request(
                        msg["verb"], msg.get("body", {}), mbins
                    )
                    rchan._send_msg(
                        {"id": msg["id"], "kind": "resp", "body": rbody}, rbins
                    )
                except RemoteError as e:
                    rchan._send_msg(
                        {"id": msg["id"], "kind": "err", "etype": e.etype,
                         "message": e.message}, []
                    )
            elif kind in ("resp", "err") and msg.get("id") == rid:
                if kind == "err":
                    return {
                        "ok": False, "transcript": "", "prints": [],
                        "outputs": {}, "outputs_skipped": {},
                        "error": {"etype": msg.get("etype", "RunnerError"),
                                  "message": msg.get("message", "")},
                    }
                return msg.get("body", {})

    def _crash_result(self, proc) -> dict:
        # The ChannelClosed path arrives here without ever polling, so
        # give the exit status a moment to land before reporting it.
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
        code = proc.returncode if proc.returncode is not None else "(unreaped)"
        return {
            "ok": False, "transcript": "", "prints": [],
            "outputs": {}, "outputs_skipped": {},
            "error": {"etype": "RunnerCrash",
                      "message": f"runner exited {code} without a result"},
        }

    def _kill(self, proc) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def _reap(self, proc) -> None:
        if proc.poll() is None:
            self._kill(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def main() -> None:
    fd = int(sys.argv[1])
    root = Path(sys.argv[2])
    sock = socketlib.socket(fileno=fd)
    channel = Channel(sock)
    Supervisor(channel, root)
    channel.hello_send()
    channel.serve()
    channel.close()


if __name__ == "__main__":
    main()
