"""Layer pip packages into a rootfs as cross-built arm64/amd64 wheels.

The base image ships bare Python; a workspace that wants numpy/pandas/etc.
needs them *in the guest*, not the host. We fetch prebuilt Linux wheels
for the guest's arch — the C-extension ``.so``s are already compiled
inside them — and fold the unpacked tree into the rootfs ``site-packages``.

``uv pip install --target`` does the heavy lifting: it resolves the
dependency graph, cross-targets the guest platform (``--python-platform``),
downloads wheels only (``--only-binary``), and unpacks them into a target
directory — no pip in the build venv, no Linux host, no compiler. We then
copy that tree into the FileSet (scripts to ``/usr/local/bin``, everything
else to ``site-packages``).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from .cpio import FileSet

# The guest base (python:slim = Debian bookworm) has glibc 2.36, so
# declare manylinux_2_28: the generic `*-unknown-linux-gnu` target
# assumes ancient glibc (manylinux_2_17) and silently resolves YEARS-old
# versions of packages whose current wheels need 2_26/2_28 (that skew
# broke cross-executor cache unpickles before versions were pinned).
_PLATFORM = {
    "arm64": "aarch64-manylinux_2_28",
    "amd64": "x86_64-manylinux_2_28",
}


class WheelError(Exception):
    """Resolving or fetching wheels for the guest platform failed."""


def python_version_from_site(site: str) -> str:
    """Extract ``3.12`` from ``.../python3.12/site-packages``."""
    m = re.search(r"python(\d+\.\d+)", site)
    return m.group(1) if m else "3.12"


def resolve_wheels(
    packages: list[str], dest: Path, arch: str, python_version: str
) -> Path:
    """Cross-install ``packages`` for the guest into ``dest`` (unpacked)."""
    platform = _PLATFORM.get(arch)
    if platform is None:
        raise WheelError(f"no wheel platform mapping for arch {arch!r}")
    uv = shutil.which("uv")
    if uv is None:
        raise WheelError("uv not found; the packages= layer needs uv on PATH")
    cmd = [
        uv, "pip", "install",
        "--target", str(dest),
        "--python-platform", platform,
        "--python-version", python_version,
        "--only-binary", ":all:",
        *packages,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise WheelError(
            f"wheel resolution failed for {packages}:\n{proc.stderr.strip()}"
        )
    return dest


def add_target_tree(fileset: FileSet, target: Path, site: str) -> None:
    """Fold an ``uv --target`` tree into the rootfs FileSet.

    Top-level ``bin/`` scripts route to ``/usr/local/bin``; everything
    else lands under ``site-packages``. Executable bits are preserved so
    the guest loader/scripts behave.
    """
    target = Path(target)
    for p in sorted(target.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        rel = p.relative_to(target)
        if rel.parts[0] == "bin":
            dst = "usr/local/bin/" + "/".join(rel.parts[1:])
        else:
            dst = f"{site}/{rel.as_posix()}"
        perm = 0o755 if os.access(p, os.X_OK) else 0o644
        fileset.add_file(dst, p.read_bytes(), perm)
