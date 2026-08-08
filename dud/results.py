"""Host-facing result shapes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ShellResult:
    transcript: str
    exit_code: int
    cwd: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def __bool__(self) -> bool:
        return self.ok


@dataclass(frozen=True)
class ExecError:
    etype: str
    message: str
    traceback: str = ""


@dataclass(frozen=True)
class PythonResult:
    ok: bool
    transcript: str
    prints: list[dict] = field(default_factory=list)
    prints_dropped: int = 0
    outputs: dict[str, Any] = field(default_factory=dict)
    outputs_skipped: dict[str, str] = field(default_factory=dict)
    error: ExecError | None = None

    def __bool__(self) -> bool:
        return self.ok


@dataclass(frozen=True)
class Diff:
    """Workspace changes since the last rebase point.

    ``writes`` maps relative path -> content bytes; ``deletes`` lists
    relative paths removed. This is the producer-agnostic wire shape:
    scan-diff (rung 1) and overlayfs harvest (rungs 2-3) both emit it.

    ``modes`` carries the POSIX permission bits for entries in
    ``writes``, and exists because losing them makes the round trip
    quietly destructive: an agent runs ``chmod +x deploy.sh``, the
    checkpoint stores bytes, and next session the script isn't
    executable. Only paths whose mode differs from the plain-file
    default appear, so the common case stays empty and a consumer that
    ignores it behaves exactly as before.

    Symlinks and empty directories still do not round-trip; both
    producers drop them before anything reaches this shape. There is
    deliberately no raw-archive escape hatch for them: the archive is
    built from the same regular-file list, so it could not carry them
    either — and exposing it would hand a consumer guest-controlled
    setuid bits that ``modes`` masks off here.
    """

    writes: dict[str, bytes] = field(default_factory=dict)
    deletes: list[str] = field(default_factory=list)
    modes: dict[str, int] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.writes and not self.deletes

    def mode(self, path: str, default: int = 0o644) -> int:
        """Permission bits for ``path``, or the plain-file default.

        Saves every consumer the same ``modes.get(p, 0o644)``, and keeps
        the default in one place if it ever needs to change.
        """
        return self.modes.get(path, default)
