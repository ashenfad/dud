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
    """

    writes: dict[str, bytes] = field(default_factory=dict)
    deletes: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.writes and not self.deletes
