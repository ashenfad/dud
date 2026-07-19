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

import ctypes
import json
import os
import select
import signal
import socket as socketlib
import struct
import subprocess
import sys
import time
from pathlib import Path

from contextlib import nullcontext

from ..proto import (
    Channel,
    ChannelClosed,
    RemoteError,
    freeze_served,
    shutdown_served,
)
from .shell import ShellOutcome, ShellState, run_shell
from .staging import make_stage

_RUNNER_DEFAULT_TIMEOUT = 30.0
_SHELL_DEFAULT_TIMEOUT = 30.0
_CTL_TIMEOUT = 5.0  # template control handshake budget

# clock_settime plumbing for do_resync (guest PID 1 is root; the
# subprocess rung never takes this path).
_CLOCK_REALTIME = 0
_libc = ctypes.CDLL(None, use_errno=True)


class _timespec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]

# Consecutive worker-path failures before the template is presumed
# fork-hostile (a warm import left live threads; children wedge) and
# replaced. One failure can be the exec's own fault; two in a row
# without a success is the pattern of a poisoned template.
_WORKER_FAILURE_LIMIT = 2


class _WorkerChild:
    """Popen-shaped handle for a runner forked by the template.

    The child is the TEMPLATE's child, not ours, so waitpid is off the
    table — liveness is signal-0, exit codes are unknowable (the pump
    only needs alive/dead), and kill is the same killpg the spawn path
    uses (the child setsid's after fork)."""

    def __init__(self, pid: int):
        self.pid = pid
        self.returncode: int | None = None

    def poll(self):
        try:
            os.kill(self.pid, 0)
            return None
        except (ProcessLookupError, PermissionError):
            self.returncode = -1
            return -1

    def wait(self, timeout: float | None = None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("view-worker", timeout)
            time.sleep(0.01)
        return self.returncode


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
        # View-worker template (VM rung only): warms in the background;
        # view execs route through it once ready, spawn-path until then.
        self._template: tuple[subprocess.Popen, socketlib.socket] | None = None
        self._template_ready = False
        self._worker_failures = 0
        self._start_template()
        channel.handler = self.handle

    def rebind(self, channel: Channel) -> None:
        """Attach to a fresh channel after a freeze/thaw cycle. All the
        warm state — staging, shell env, the fork template — carries
        over; only the transport is new."""
        self.channel = channel
        channel.handler = self.handle

    # ---- view-worker template ----------------------------------------

    def _start_template(self) -> None:
        """Spawn the warm fork-template (dud.guest.template). Best
        effort and VM-rung only: a failed or absent template just
        means view execs stay on the spawn path."""
        if os.getpid() != 1:
            return
        try:
            ours, theirs = socketlib.socketpair()
            proc = subprocess.Popen(
                [sys.executable, "-m", "dud.guest.template",
                 str(theirs.fileno())],
                pass_fds=(theirs.fileno(),),
                start_new_session=True,
                env=dict(self._boot_env),
            )
            theirs.close()
            self._template = (proc, ours)
            self._template_ready = False
        except Exception:
            self._template = None

    def _drop_template(self) -> None:
        if self._template is None:
            return
        proc, ctl = self._template
        self._template = None
        self._template_ready = False
        try:
            ctl.close()
        except OSError:
            pass
        try:
            os.kill(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def _worker_state(self) -> str:
        if self._template is None:
            return "off"
        proc, ctl = self._template
        if proc.poll() is not None:
            # Died (crash, OOM): re-warm rather than staying cold —
            # worst case is one cheap background spawn per exec.
            self._drop_template()
            self._start_template()
            return "warming" if self._template is not None else "off"
        if not self._template_ready:
            # The readiness byte arrives whenever warm-up finishes.
            ready, _, _ = select.select([ctl], [], [], 0)
            if ready:
                try:
                    self._template_ready = ctl.recv(1) == b"R"
                except OSError:
                    self._drop_template()
                    return "off"
                if not self._template_ready:  # EOF: template died
                    self._drop_template()
                    return "off"
        return "ready" if self._template_ready else "warming"

    def _fork_from_template(self, cwd: str, env: dict):
        """Ask the template for a forked runner on a fresh socketpair.
        Returns (parent_socket, child_handle) or None to fall back to
        the spawn path (any control-channel trouble drops the template;
        a fresh one is started for later execs)."""
        if self._worker_state() != "ready":
            return None
        _proc, ctl = self._template
        parent, child = socketlib.socketpair()
        try:
            payload = json.dumps({"cwd": cwd, "env": env}).encode()
            ctl.settimeout(_CTL_TIMEOUT)
            # fd + 4-byte length ride one sendmsg (atomic at this size);
            # the payload follows via sendall — a single sendmsg of
            # header+payload can send PARTIALLY under a timeout (seen
            # at ~8 KB on macOS, ~208 KB in the guest), which would
            # wedge the template mid-frame on any large session env.
            sent = ctl.sendmsg(
                [struct.pack(">I", len(payload))],
                [(socketlib.SOL_SOCKET, socketlib.SCM_RIGHTS,
                  struct.pack("i", child.fileno()))],
            )
            if sent != 4:
                raise OSError(f"short ctl header send ({sent}/4)")
            ctl.sendall(payload)
            pid_bytes = b""
            while len(pid_bytes) < 8:
                chunk = ctl.recv(8 - len(pid_bytes))
                if not chunk:
                    raise OSError("template EOF")
                pid_bytes += chunk
            ctl.settimeout(None)
        except OSError:
            parent.close()
            child.close()
            self._drop_template()
            self._start_template()
            return None
        child.close()
        return parent, _WorkerChild(int.from_bytes(pid_bytes, "big"))

    # ---- dispatch ----------------------------------------------------

    def handle(self, verb: str, body: dict, bins: list[bytes]):
        method = getattr(self, f"do_{verb.replace('.', '_')}", None)
        if method is None:
            raise ValueError(f"unknown verb {verb!r}")
        return method(body, bins)

    # ---- verbs -------------------------------------------------------

    def do_ping(self, body, bins):
        return {"pong": True, "workspace": str(self.work),
                "staging": self.stage.kind,
                "view_worker": self._worker_state()}, []

    def do_shutdown(self, body, bins):
        shutdown_served()

    def do_freeze(self, body, bins):
        """The host is about to snapshot this VM (see the firecracker
        rung's freeze/thaw). Flush block-backed state so the frozen
        disk image is consistent, ack, and hand the serve loop back to
        init's redial-and-wait posture. VM rung only: elsewhere there
        is no machine to snapshot and no redial loop to return to."""
        if os.getpid() != 1:
            raise ValueError("freeze requires the VM rung (guest PID 1)")
        os.sync()
        freeze_served()

    def do_resync(self, body, bins):
        """Post-thaw fixup, host-initiated on the first request after a
        redial: set the wall clock (frozen guests wake with the clock
        stopped at snapshot time) and replace the fork template (its
        interpreter pre-dates the snapshot, so clones of one snapshot
        would otherwise share PRNG state across forked workers)."""
        if os.getpid() != 1:
            return {}, []
        epoch = float(body["epoch"])
        ts = _timespec(int(epoch), int((epoch % 1.0) * 1e9))
        rc = _libc.clock_settime(_CLOCK_REALTIME, ctypes.byref(ts))
        if rc != 0:
            # Non-fatal (the session works, timestamps lie), but a
            # silently stale clock is undebuggable — leave a trace.
            sys.stderr.write(
                f"[dud] resync clock_settime failed: errno "
                f"{ctypes.get_errno()}\n"
            )
        self._drop_template()
        self._worker_failures = 0
        self._start_template()
        return {}, []

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
            # The kill sweep above took the template with it (it's just
            # another non-PID-1 process — that's the hygiene contract).
            # Re-warm for the next session while it boots/pushes.
            self._drop_template()
            self._worker_failures = 0
            self._start_template()
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
        env = dict(self.shell.env)
        env["DUD_WORKSPACE"] = str(self.work)
        cwd = self.shell.cwd if os.path.isdir(self.shell.cwd) else str(self.work)
        with guard:
            # View execs (fs_readonly) route through the warm template
            # when it's ready — fork beats spawn+imports by ~0.4 s on
            # the DS stack. Anything else, or a template that isn't
            # there yet, takes the identical spawn path.
            forked = (self._fork_from_template(cwd, env)
                      if body.get("fs_readonly") else None)
            result = self._exec_once(forked, cwd, env, run_body, timeout)
            if forked is not None:
                self._note_worker_outcome(result)
                err = (result or {}).get("error")
                if err and err.get("etype") == "RunnerCrash":
                    # A worker child that died without ever answering
                    # (fork-env fragility: setsid pid collisions and
                    # kin — observed on loaded CI runners). The spawn
                    # path is the structural fallback, so take it once
                    # for THIS exec rather than surfacing a crash the
                    # caller didn't earn. Same at-most-once-observed
                    # doctrine as executor recovery: state can't have
                    # moved (fs_readonly), hostcalls may repeat.
                    # Timeouts are excluded — user code ran there.
                    result = self._exec_once(None, cwd, env, run_body,
                                             timeout)
        return result, []

    def _exec_once(self, forked, cwd: str, env: dict, run_body: dict,
                   timeout: float) -> dict:
        """One runner lifecycle: connect (forked worker or fresh
        spawn), send the run request, pump to a result, clean up."""
        if forked is not None:
            parent, proc = forked
        else:
            parent, child = socketlib.socketpair()
            proc = subprocess.Popen(
                [sys.executable, "-m", "dud.guest.runner",
                 str(child.fileno())],
                pass_fds=(child.fileno(),),
                cwd=cwd,
                env=env,
                start_new_session=True,
            )
            child.close()
        rchan = Channel(parent)
        rchan._next_id += 1
        rid = rchan._next_id
        try:
            rchan._send_msg(
                {"id": rid, "kind": "req", "verb": "run", "body": run_body}, []
            )
            return self._pump_runner(rchan, rid, proc, timeout)
        except OSError:
            # EPIPE at send: the runner died before taking the request.
            # Crash-shaped, so the breaker and the retry above see it —
            # a raw BrokenPipeError reply would look like a wire bug.
            return self._crash_result(proc)
        finally:
            rchan.close()
            self._reap(proc)

    def _note_worker_outcome(self, result: dict) -> None:
        """Circuit breaker: a template whose children keep timing out
        or crashing without ever answering is presumed fork-hostile (a
        warm import — torch, grpc — left live threads, so forked
        children deadlock). Control-channel checks can't see this: the
        fork *succeeds*, the child just never serves. Two consecutive
        such failures replace the template; execs meanwhile (and any
        exec while it re-warms) take the spawn path, which always
        works — degradation stays structural."""
        err = (result or {}).get("error")
        if err and err.get("etype") in ("Timeout", "RunnerCrash"):
            self._worker_failures += 1
            if self._worker_failures >= _WORKER_FAILURE_LIMIT:
                self._worker_failures = 0
                self._drop_template()
                self._start_template()
        else:
            self._worker_failures = 0

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
        if isinstance(proc, _WorkerChild):
            # The template owns its children's lifecycle (SIGCHLD
            # reaper); we just make sure the group is dead. Waiting
            # here would race the template's reap for no benefit.
            self._kill(proc)
            return
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
