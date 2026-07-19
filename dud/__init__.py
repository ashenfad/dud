"""dud: a dumb firecracker.

Real, disposable machines for versioned agent workspaces. Tree in,
execute against a real filesystem, diff out — versioning stays in the
layer above (see DESIGN.md).

The front door is :func:`session` (backend selection + pooling in one
place); everything else here is a lazy re-export of the blessed
surface. Deep imports (``dud.backends.vfkit.VfkitSession``, ...) keep
working and always will.
"""

from __future__ import annotations

from typing import Any

from .backends.subprocess import Session
from .errors import DudError
from .proto import PROTO_VERSION, ProtocolError, RemoteError
from .results import Diff, ExecError, PythonResult, ShellResult
from .values import NotRepresentable

__version__ = "0.0.1"

__all__ = [
    "session",
    "Session",
    "VfkitSession",
    "FirecrackerSession",
    "scratch_master",
    "blank_ext4",
    "Diff",
    "ExecError",
    "PythonResult",
    "ShellResult",
    "DudError",
    "SessionLost",
    "IsolationUnavailable",
    "NotRepresentable",
    "ProtocolError",
    "RemoteError",
    "PROTO_VERSION",
]

# Lazy exports (PEP 562): `import dud` must stay light — the VM rung
# and image machinery load only when reached for.
_LAZY = {
    "VfkitSession": ("dud.backends.vfkit", "VfkitSession"),
    "FirecrackerSession": ("dud.backends.firecracker", "FirecrackerSession"),
    "IsolationUnavailable": ("dud.errors", "IsolationUnavailable"),
    "SessionLost": ("dud.errors", "SessionLost"),
    "scratch_master": ("dud.images.scratch", "scratch_master"),
    "blank_ext4": ("dud.images.scratch", "blank_ext4"),
}


def __getattr__(name: str):
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module 'dud' has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(target[0]), target[1])


def session(
    backend: str = "subprocess",
    *,
    pooled: bool = False,
    state: str | None = None,
    **kwargs: Any,
):
    """Open a session on the chosen rung — the one blessed entry point.

    - ``backend="subprocess"``: the rung-1 guest as a host process.
      Real bash/python/files, ZERO isolation (own-machine posture).
    - ``backend="vfkit"``: a disposable macOS microVM (HVF).
    - ``backend="firecracker"``: a disposable Linux/KVM microVM.
    - ``backend="vm"``: the best VM rung for this host — vfkit on
      macOS, firecracker on Linux. Config written against ``"vm"``
      never changes as rungs land.

    ``pooled=True`` (VM rungs only) acquires from the process-wide
    warm pool instead of booting; ``state`` is the content tag for
    park affinity — a parked VM already holding that exact tree comes
    back with ``resumed=True`` and the caller skips its push. Extra
    kwargs go to the backend constructor.
    """
    if backend == "vm":
        # The best VM rung for this host: configs written against
        # "vm" survive new rungs landing.
        import platform

        backend = "vfkit" if platform.system() == "Darwin" else "firecracker"
    if backend == "vfkit":
        if pooled:
            from .backends.pool import shared_pool
            from .backends.vfkit import VfkitSession

            return shared_pool(VfkitSession).acquire(state=state, **kwargs)
        if state is not None:
            raise ValueError("state= is park affinity; it requires pooled=True")
        from .backends.vfkit import VfkitSession

        return VfkitSession(**kwargs)
    if backend == "firecracker":
        from .backends.firecracker import FirecrackerSession

        if pooled:
            from .backends.pool import shared_pool

            return shared_pool(FirecrackerSession).acquire(
                state=state, **kwargs
            )
        if state is not None:
            raise ValueError("state= is park affinity; it requires pooled=True")
        return FirecrackerSession(**kwargs)
    if backend == "subprocess":
        if pooled or state is not None:
            raise ValueError("pooling is a VM-rung concept (rung 1 has no boot to skip)")
        return Session(**kwargs)
    raise ValueError(
        f"unknown backend {backend!r} (subprocess | vfkit | firecracker | vm)"
    )
