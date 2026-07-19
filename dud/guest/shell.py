"""Real bash with terminal-like session state.

Each ``exec_shell`` runs a fresh bash, but cwd and env persist across
calls (PLAN.md decision #2): the supervisor wraps every script with an
EXIT trap that dumps final cwd and env to side files, then replays
them into the next invocation. ``cd`` sticks, ``export`` sticks, and —
matching real-terminal behavior — they stick even when the script
exits nonzero.

Transcript is stdout+stderr merged (terminal-faithful, the termish
precedent). Timeout kills the whole process group.
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

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

_DROP_VARS = {"__DUD_CWD__", "__DUD_ENV__", "_", "SHLVL", "OLDPWD"}
_MAX_ENV_ENTRY = 96 * 1024  # comfortably under Linux MAX_ARG_STRLEN


@dataclass
class ShellState:
    cwd: str
    env: dict[str, str] = field(default_factory=lambda: dict(os.environ))


@dataclass
class ShellOutcome:
    transcript: str
    exit_code: int
    timed_out: bool = False


def run_shell(
    state: ShellState, script: str, timeout: float, workspace: str
) -> ShellOutcome:
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

        proc = subprocess.Popen(
            ["bash", "--noprofile", "--norc", str(script_file)],
            cwd=state.cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            out, _ = proc.communicate(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, _ = proc.communicate()
            timed_out = True

        if not timed_out:
            _replay(state, cwd_file, env_file)

        return ShellOutcome(
            transcript=out.decode(errors="replace"),
            exit_code=proc.returncode if not timed_out else 124,
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
