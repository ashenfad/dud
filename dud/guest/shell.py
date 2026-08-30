"""Real bash with terminal-like session state.

Each ``exec_shell`` runs a fresh bash, but cwd and env persist across
calls (PLAN.md decision #2): the supervisor wraps every script with an
EXIT trap that dumps final cwd and env to side files, then replays
them into the next invocation. ``cd`` sticks, ``export`` sticks, and —
matching real-terminal behavior — they stick even when the script
exits nonzero.

Transcript is stdout+stderr merged (terminal-faithful, the termish
precedent). Timeout kills the whole process group.

Output is read incrementally and bounded as it arrives, rather than
collected with ``communicate()``. That is not a style preference: the
supervisor holding this buffer is PID 1 on a VM rung, so an unbounded
one turns any chatty script into a memory attack on the machine (one
second of ``yes`` was 200 MB), and a ``communicate()`` with no deadline
hands a background process the power to wedge the guest for as long as
it lives. See :class:`_Transcript` and :func:`_pump`.
"""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import emit as emit_mod
from .runner import _CAP_STDOUT

# The env dump uses only builtins (compgen/printf): the external `env`
# binary would need the whole environment passed through execve, whose
# Linux per-string cap (MAX_ARG_STRLEN, 128 KB) silently broke the
# snapshot — and so env persistence — the moment any exported var got
# big. Same NUL-delimited KEY=VAL wire format either way.
_TRAP = (
    "trap '__dud_rc=$?; pwd > \"$__DUD_CWD__\"; "
    "for __dud_v in $(compgen -e); do "
    "printf \"%s=%s\\0\" \"$__dud_v\" \"${!__dud_v}\"; done "
    "> \"$__DUD_ENV__\"' EXIT\n"
)

# The emit plumbing is per-exec: an fd number and a lock path from
# one call are meaningless (and misleading) in the next, so they
# must never survive into the persisted environment.
_DROP_VARS = {"__DUD_CWD__", "__DUD_ENV__", "_", "SHLVL", "OLDPWD",
              emit_mod.FD_VAR, emit_mod.LOCK_VAR}
_MAX_ENV_ENTRY = 96 * 1024  # comfortably under Linux MAX_ARG_STRLEN

# The transcript ceiling, imported rather than restated: this is the
# same concept as the python runner's transcript and the two drifting
# apart is exactly the failure this bound was added to fix (the python
# path had a cap from the start; bash had none, so the identical field
# on the identical result object differed by 200x).
_CAP_TRANSCRIPT = _CAP_STDOUT

_DRAIN_BUDGET = 0.5  # post-kill: how long a dead script's tail may take
_REAP_BUDGET = 5.0   # waitpid on a killed script
_POLL = 0.25         # how often to re-check a script that is quiet


@dataclass
class ShellState:
    cwd: str
    env: dict[str, str] = field(default_factory=lambda: dict(os.environ))


@dataclass
class ShellOutcome:
    transcript: str
    exit_code: int
    timed_out: bool = False


class _Transcript:
    """The script's output, bounded as it arrives.

    Keeps the HEAD and counts the rest, matching how the runner caps
    the python transcript: a terminal session is read from the top, and
    what the script was doing is worth more than the ten-thousandth
    line of its output. (The supervisor's ``_Spill`` keeps the TAIL
    instead, for the opposite reason — it is evidence from a process
    that died without answering, where the last thing printed says how
    far it got. Two different questions, two different halves.)

    Draining continues past the cap rather than closing the pipe. A
    writer that fills the 64 KB pipe buffer blocks, so a script whose
    output we stopped reading would stall until its timeout instead of
    running to completion — turning a memory bound into a behavior
    change, which is not a trade worth making.
    """

    def __init__(self, cap: int = _CAP_TRANSCRIPT):
        self._cap = cap
        self._buf = bytearray()
        self.dropped = 0

    def read(self, fd: int) -> bool:
        """One read; False at EOF or on a dead pipe."""
        try:
            chunk = os.read(fd, 65536)
        except (OSError, ValueError):
            return False
        if not chunk:
            return False
        room = max(0, self._cap - len(self._buf))
        if room:
            self._buf += chunk[:room]
        self.dropped += len(chunk) - min(room, len(chunk))
        return True

    def text(self) -> str:
        # Bytes, not chars: the cap is applied to what was read, and
        # naming the unit honestly beats matching the runner's wording
        # for a number that means something slightly different.
        out = self._buf.decode(errors="replace")
        if self.dropped:
            out += (f"\n… [truncated at {self._cap} bytes; "
                    f"{self.dropped} more not captured]")
        return out


def _killpg(proc: subprocess.Popen) -> None:
    """Kill the script's whole process group.

    ``PermissionError`` is caught alongside the obvious
    ``ProcessLookupError`` because Darwin raises EPERM — not ESRCH —
    when the group's only remaining member is a zombie, which is the
    normal state at a timeout whose script already exited. Uncaught, it
    escaped ``run_shell`` and reached the host as a ``PermissionError``
    from ``exec_shell`` instead of a timeout. ``supervisor._kill``
    already caught both; this path had not learned it yet.
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _drain(out: _Transcript, fd: int, budget: float = _DRAIN_BUDGET) -> None:
    """Take what the pipe still holds, now the script is no longer in it.

    Used at both exits — the script was killed at its deadline, or it
    ended on its own while something it started kept the fd. Either
    way the script has written everything it is going to, so this reads
    what is there and stops rather than waiting for an end that belongs
    to somebody else.

    Bounded by a deadline as well as by EOF, exactly as the
    supervisor's ``_Spill.drain`` is and for exactly the same reason: a
    process holding the write end can supply data indefinitely, and an
    exec must still answer. The absence of this bound is what let one
    ``setsid`` background process wedge the supervisor — single
    threaded, PID 1 — for as long as that process lived.
    """
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        try:
            ready, _, _ = select.select([fd], [], [], 0)
        except (OSError, ValueError):
            return
        if not ready or not out.read(fd):
            return


class _Emits:
    """Newline-delimited emit records, dispatched as they arrive.

    Live, not collected: an emit from bash reaches the host mid-exec
    exactly as one from the Python runner does. That is the whole
    point of routing them through the pipe the loop already watches
    rather than through a file read at the end.

    The records come from ``dud-emit`` (see :mod:`dud.guest.emit`) and
    are relayed upstream unopened. This layer knows nothing about the
    value codec — it validates the shape and forwards, which is what
    makes an emit from bash and one from Python indistinguishable to
    the host.
    """

    #: A record no writer could legitimately produce: `dud-emit` caps
    #: itself well below this, so passing it means somebody is writing
    #: to the fd directly and the buffer would otherwise grow forever.
    _CAP = (1 << 20) + (1 << 16)

    def __init__(self, on_emit):
        self._on_emit = on_emit
        self._buf = bytearray()
        self.dispatched = 0
        self.dropped = 0

    def read(self, fd: int) -> bool:
        """One read; False at EOF or on a dead pipe."""
        try:
            chunk = os.read(fd, 65536)
        except (OSError, ValueError):
            return False
        if not chunk:
            return False
        self._buf += chunk
        self._flush()
        if len(self._buf) > self._CAP:
            # An unterminated record past any legitimate size. Drop what
            # has accumulated rather than let one malformed writer size
            # the supervisor's memory.
            self._buf.clear()
            self.dropped += 1
        return True

    def _flush(self) -> None:
        while b"\n" in self._buf:
            line, _, rest = self._buf.partition(b"\n")
            self._buf = bytearray(rest)
            if line.strip():
                self._dispatch(bytes(line))

    def _dispatch(self, line: bytes) -> None:
        try:
            body = json.loads(line.decode())
        except (ValueError, UnicodeDecodeError):
            self.dropped += 1
            return
        if (not isinstance(body, dict) or not isinstance(body.get("name"), str)
                or not isinstance(body.get("value"), dict)):
            # Only `dud-emit` should be writing here; anything else is
            # refused rather than relayed, so a stray write into the fd
            # cannot put an arbitrary body on the host's wire.
            self.dropped += 1
            return
        try:
            self._on_emit({"name": body["name"], "value": body["value"]})
            self.dispatched += 1
        except Exception:  # noqa: BLE001 — a failed relay is not the script's fault
            self.dropped += 1

    def drain(self, fd: int, budget: float = _DRAIN_BUDGET) -> None:
        """Take the records still in the pipe now the script is done.

        Bounded exactly as the transcript's drain is, and for the same
        reason — but load-bearing for a different one: without it, an
        emit fired by the last line of a script would be lost to the
        race between the write and the exit.
        """
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            try:
                ready, _, _ = select.select([fd], [], [], 0)
            except (OSError, ValueError):
                return
            if not ready or not self.read(fd):
                return


def _pump(proc: subprocess.Popen, timeout: float,
          emit_fd: int | None = None, on_emit=None) -> tuple[_Transcript, bool]:
    """Read the script's output until it is done, or kill it at the
    deadline. Returns ``(transcript, timed_out)``.

    "Done" is the script's own exit, NOT end-of-pipe. Those differ
    whenever the script leaves something running that inherited its
    stdout — ``nohup server &``, the most ordinary thing an agent does
    — and waiting for the pipe there means waiting for that daemon to
    die. A perfectly successful one-second script used to burn its
    entire timeout and come back ``timed_out=True`` because of it.
    Anything the script itself wrote has already been read by then;
    what stays open is somebody else's fd.

    Which makes the *placement* of the exit check load-bearing, not
    incidental — see the comment on it below. A survivor that writes
    without pause keeps the pipe readable forever, and the first cut of
    this loop only looked at ``poll()`` on a pass that read nothing.
    That worked for a silent ``sleep 30 &`` and failed for the logging
    dev server that motivated the whole function.
    """
    fd = proc.stdout.fileno()
    out = _Transcript()
    emits = _Emits(on_emit) if (emit_fd is not None and on_emit) else None
    open_pipe = True
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _killpg(proc)
            _drain(out, fd)
            if emits is not None:
                emits.drain(emit_fd)
            return out, True
        # Every pass, and BEFORE the read rather than after it. A
        # survivor that writes CONTINUOUSLY — `nohup npm run dev &`, a
        # dev server logging as it boots — keeps select() readable with
        # no gap, so a poll placed after a "did we read anything" branch
        # is a poll that never runs, and the finished script times out
        # anyway. The quiet survivor and the chatty one are the same
        # bug; only the chatty one defeats a check that waits for
        # silence to run.
        if proc.poll() is not None:
            if open_pipe:
                # Bounded, by the same argument as the post-kill drain:
                # the script has written everything it is going to, so
                # whatever is still arriving belongs to whoever
                # inherited the fd. Take what is there and stop.
                _drain(out, fd)
            if emits is not None:
                emits.drain(emit_fd)
            return out, False
        watch = [fd] if open_pipe else []
        if emits is not None:
            watch.append(emit_fd)
        if watch:
            try:
                ready, _, _ = select.select(watch, [], [], min(remaining, _POLL))
            except (OSError, ValueError):
                # An fd we can no longer watch. Stop watching it and
                # keep waiting on the process rather than returning: the
                # only non-timeout exit above is one where poll() has
                # answered, which is what keeps `returncode` a number.
                ready, open_pipe, emits = (), False, None
            if open_pipe and fd in ready and not out.read(fd):
                open_pipe = False  # EOF: every writer has let go
            if emits is not None and emit_fd in ready:
                # Relaying costs a round trip to the host, and that is
                # ours rather than the script's — so it does not come
                # out of the script's timeout. Same rule the wire
                # applies to a hostcall served mid-exec: a caller is not
                # charged for our own slowness.
                started = time.monotonic()
                if not emits.read(emit_fd):
                    emits = None  # EOF on the emit side; the script may run on
                deadline += time.monotonic() - started
        else:
            # The script closed its own stdout but is still running
            # (`exec >&-; work`). Nothing to select on, so poll it.
            time.sleep(min(remaining, _POLL))


def run_shell(
    state: ShellState, script: str, timeout: float, workspace: str,
    on_emit=None,
) -> ShellOutcome:
    """Run one script. ``on_emit`` receives each ``dud-emit`` record as
    it arrives, already validated, for relay upstream; without it the
    emit channel is simply not offered and ``dud-emit`` says so."""
    with tempfile.TemporaryDirectory(prefix="dud-sh-") as td:
        cwd_file = Path(td) / "cwd"
        env_file = Path(td) / "env"
        script_file = Path(td) / "script.sh"
        script_file.write_text(_TRAP + script + "\n")

        env = dict(state.env)
        env["__DUD_CWD__"] = str(cwd_file)
        env["__DUD_ENV__"] = str(env_file)
        env["DUD_WORKSPACE"] = workspace

        if not os.path.isdir(state.cwd):
            state.cwd = workspace

        # The emit channel, offered only when somebody is listening. An
        # inherited fd rather than a path: bash hands it to every child
        # and subshell for free, so `dud-emit` inside `$(...)`, a
        # pipeline, or a backgrounded job all reach the same pipe.
        emit_r = emit_w = None
        pass_fds: tuple[int, ...] = ()
        if on_emit is not None:
            emit_r, emit_w = os.pipe()
            os.set_inheritable(emit_w, True)
            lock = Path(td) / "emit.lock"
            lock.touch()
            env[emit_mod.FD_VAR] = str(emit_w)
            env[emit_mod.LOCK_VAR] = str(lock)
            pass_fds = (emit_w,)

        proc = subprocess.Popen(
            ["bash", "--noprofile", "--norc", str(script_file)],
            cwd=state.cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            pass_fds=pass_fds,
        )
        try:
            if emit_w is not None:
                # The child holds the write end now. Ours has to go, or
                # the pipe never reaches EOF and the reader waits on a
                # writer that is us.
                os.close(emit_w)
                emit_w = None
            out, timed_out = _pump(proc, timeout, emit_r, on_emit)
        finally:
            proc.stdout.close()
            for fd in (emit_w, emit_r):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

        # waitpid, not communicate: reaping does not touch the pipe, so
        # a process still holding the write end cannot stall it. Bounded
        # anyway — nothing after a group kill is worth blocking on.
        try:
            proc.wait(timeout=_REAP_BUDGET)
        except subprocess.TimeoutExpired:
            _killpg(proc)

        if not timed_out:
            # Only after a clean exit. A timeout is SIGKILL, so the EXIT
            # trap never ran, and it can be killed BETWEEN its two
            # writes — replaying then would take a cwd with no env and
            # call the pair a session state. Nothing to restore is the
            # honest reading of a script that was shot.
            _replay(state, cwd_file, env_file)

        return ShellOutcome(
            transcript=out.text(),
            exit_code=(proc.returncode if not timed_out else 124),
            timed_out=timed_out,
        )


def _replay(state: ShellState, cwd_file: Path, env_file: Path) -> None:
    try:
        cwd = cwd_file.read_text().strip()
        if cwd and os.path.isdir(cwd):
            state.cwd = cwd
    except OSError:
        pass
    try:
        raw = env_file.read_bytes()
    except OSError:
        return
    env: dict[str, str] = {}
    for entry in raw.split(b"\0"):
        # A single env string past Linux's execve cap (MAX_ARG_STRLEN,
        # 128 KB) can't cross any later spawn on the VM rung — carrying
        # it would E2BIG every subsequent shell/python call. It drops
        # ALONE (uniform on every rung for conformance parity); big
        # data belongs in workspace files, not the environment.
        if len(entry) > _MAX_ENV_ENTRY:
            continue
        if b"=" in entry:
            k, _, v = entry.partition(b"=")
            key = k.decode(errors="replace")
            if key not in _DROP_VARS:
                env[key] = v.decode(errors="replace")
    if env:
        state.env = env
