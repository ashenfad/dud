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

from collections.abc import Callable
from typing import Any

from .backends.base import public_methods
from .backends.subprocess import Session
from .errors import DudError
from .proto import (
    PROTO_VERSION,
    FrameTooLarge,
    ProtocolError,
    RemoteError,
)
from .results import Diff, ExecError, PythonResult, ShellResult
from .values import NotRepresentable, ValueTooLarge

__all__ = [
    "__version__",
    "session",
    "Session",
    "public_methods",
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
    "PolicyError",
    "NotRepresentable",
    "ValueTooLarge",
    "ProtocolError",
    "FrameTooLarge",
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
    "PolicyError": ("dud.errors", "PolicyError"),
    "scratch_master": ("dud.images.scratch", "scratch_master"),
    "blank_ext4": ("dud.images.scratch", "blank_ext4"),
}


def _installed_version() -> str:
    """Read the version from installed package metadata.

    Derived, never written down here: a literal is a second place to
    bump at release time, and the one that used to live here spent two
    releases reporting 0.0.1 because only pyproject got touched.
    Lazy for the same reason as everything else in ``_LAZY`` —
    ``importlib.metadata`` is not free, and almost nobody asks.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("dud")
    except PackageNotFoundError:
        # Imported from a source tree that was never installed (a bare
        # PYTHONPATH run). Say so rather than inventing a number.
        return "0+unknown"


def __getattr__(name: str):
    if name == "__version__":
        return _installed_version()
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
    # ---- what the guest may reach, and what interprets what it makes.
    # Named rather than left to **kwargs because these ARE the
    # extension points: a seam nobody can find in `help()` teaches
    # nothing, and `outputs_hook` in particular was moved onto the
    # session so the mechanism would be visible in a signature.
    host_objects: dict[str, Any] | None = None,
    allow: dict[str, set[str]] | None = None,
    cache: dict[str, bytes] | None = None,
    on_emit: Callable[[str, Any], None] | None = None,
    outputs_hook: str | None = None,
    render_hook: str | None = None,
    # ---- what gets booted (VM rungs; rung 1 has no image)
    image: str | None = None,
    packages: list[str] | None = None,
    memory_mib: int | None = None,
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
    back with ``resumed=True`` and the caller skips its push.

    The named kwargs above are the ones worth discovering; the rest of
    a backend's constructor (``kernel``, ``cpus``, ``medium``,
    ``debs``, ``disks``, ``scratch``, ``arch``, ``home``,
    ``workspace``, ``boot_timeout``) still passes through unchanged.
    """
    if backend == "vm":
        # The best VM rung for this host: configs written against
        # "vm" survive new rungs landing.
        import platform

        backend = "vfkit" if platform.system() == "Darwin" else "firecracker"

    # Only what the caller actually named. None means "unspecified"
    # for every one of these, and forwarding it would be wrong rather
    # than merely noisy: `image=None` would override the backend's own
    # default instead of leaving it alone.
    named = {
        "host_objects": host_objects, "allow": allow, "cache": cache,
        "on_emit": on_emit, "outputs_hook": outputs_hook,
        "render_hook": render_hook, "image": image, "packages": packages,
        "memory_mib": memory_mib,
    }
    opts = {k: v for k, v in named.items() if v is not None}
    opts.update(kwargs)

    if backend == "subprocess":
        if pooled or state is not None:
            raise ValueError(
                "pooling is a VM-rung concept (rung 1 has no boot to skip)"
            )
        return Session(**opts)

    if backend == "vfkit":
        from .backends.vfkit import VfkitSession as session_cls
    elif backend == "firecracker":
        from .backends.firecracker import FirecrackerSession as session_cls
    else:
        raise ValueError(
            f"unknown backend {backend!r} "
            f"(subprocess | vfkit | firecracker | vm)"
        )

    # One copy of the pooled/state rule, rather than one per rung: the
    # rungs became interchangeable here when they got a shared base,
    # and three copies of a two-line policy is how they drift.
    if pooled:
        from .backends.pool import shared_pool

        return shared_pool(session_cls).acquire(state=state, **opts)
    if state is not None:
        raise ValueError("state= is park affinity; it requires pooled=True")
    return session_cls(**opts)
