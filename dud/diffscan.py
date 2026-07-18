"""Rung-1 workspace staging: baseline copy + content-hash scan diff.

macOS has no overlayfs, so the subprocess backend keeps a pristine
``baseline/`` beside the mutable ``work/`` tree and diffs by content
hash (PLAN.md decision #1). The wire format is producer-agnostic — a
tar of changed/added files plus an explicit delete list — so the
overlayfs rungs emit the identical shape from their upperdir.

Copies are cheap at agent-workspace scale (MBs). Symlinks are not
followed and not preserved (v0); empty directories do not round-trip
through diffs (files imply their parents).
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import tarfile
from pathlib import Path


def _hash_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


# Derived state real CPython writes into the tree as a side effect of
# imports. It is never part of the workspace contract: under the VFS
# executors it doesn't exist at all, and letting it into diffs both
# poisons read-only views (a GET that merely IMPORTS a workspace module
# would "write") and commits bytecode junk into the store above.
_IGNORE_DIRS = {"__pycache__"}
_IGNORE_SUFFIXES = (".pyc", ".pyo")


def index_tree(root: Path) -> dict[str, str]:
    """relpath -> sha256 for every regular file under root."""
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS]
        for name in filenames:
            if name.endswith(_IGNORE_SUFFIXES):
                continue
            p = Path(dirpath) / name
            if p.is_symlink() or not p.is_file():
                continue
            out[str(p.relative_to(root))] = _hash_file(p)
    return out


def scan_diff(work: Path, baseline: Path) -> tuple[list[str], list[str]]:
    """(writes, deletes) of work relative to baseline, by content."""
    wi, bi = index_tree(work), index_tree(baseline)
    writes = sorted(p for p, h in wi.items() if bi.get(p) != h)
    deletes = sorted(p for p in bi if p not in wi)
    return writes, deletes


def make_tar(root: Path, paths: list[str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for rel in paths:
            tf.add(root / rel, arcname=rel, recursive=False)
    return buf.getvalue()


def extract_tar(data: bytes, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
        tf.extractall(dest, filter="data")


def sync_copy(src: Path, dst: Path) -> None:
    """Make dst an exact copy of src (used for reset and rebase)."""
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=False)


def clear_tree(root: Path) -> None:
    """Empty root without removing it."""
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
