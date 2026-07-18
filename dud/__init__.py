"""dud: a dumb firecracker.

Real, disposable machines for versioned agent workspaces. Tree in,
execute against a real filesystem, diff out — versioning stays in the
layer above (see DESIGN.md).
"""

from .backends.subprocess import Session
from .proto import PROTO_VERSION
from .results import Diff, ExecError, PythonResult, ShellResult

__version__ = "0.0.1"

__all__ = [
    "Session",
    "Diff",
    "ExecError",
    "PythonResult",
    "ShellResult",
    "PROTO_VERSION",
]
