"""Tree-diff primitives: content-hash scan, tar shapes, tree ops.

The scan path (``index_tree``/``scan_diff``) backs rung 1's
baseline-copy staging — macOS has no overlayfs. The wire format is
producer-agnostic — a tar of changed/added files plus an explicit
delete list — and the VM rungs' overlay staging emits the identical
shape from its upperdir (see :mod:`dud.guest.staging`).

Copies are cheap at agent-workspace scale (MBs). Empty directories do
not round-trip through diffs (files imply their parents).

Symlinks do not cross the wire either, but they are *indexed and
preserved* rather than ignored — the two are not the same thing. A link
that goes unindexed reads as an absence, and a tree copy that follows
links instead of copying them changes their shape; between them a diff
reported deletes for files that existed, and rebase destroyed links in
the guest's live workspace. Not carrying something is a contract; losing
it is a bug.

Permission bits *do* survive: ``make_tar`` records them and
:class:`~dud.results.Diff` carries them out. Both producers share this
tar, so that holds on every rung without either of them knowing about
it — which is why modes cost nothing here while symlinks would cost a
change in each.
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


def index_tree(root: Path) -> dict[str, tuple[str, str, int]]:
    """relpath -> (kind, content identity, permission bits).

    ``kind`` is ``"f"`` for a regular file, whose identity is its
    sha256, or ``"l"`` for a symlink, whose identity is its target. The
    mode is part of the identity too, not decoration: ``chmod +x`` on an
    otherwise untouched file changes nothing about its content, and an
    index that only hashed bytes reported that as no change at all.

    Symlinks are indexed rather than skipped so they stop *looking
    absent*. A skipped link is a path present on disk and missing from
    the index, which the delete arithmetic in :func:`scan_diff` reads as
    a removal. Their targets still don't cross the wire — the host
    decoder takes regular files only — but a diff must not claim a file
    was deleted while it sits there.
    """
    out: dict[str, tuple[str, str, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS]
        for name in filenames:
            if name.endswith(_IGNORE_SUFFIXES):
                continue
            p = Path(dirpath) / name
            rel = str(p.relative_to(root))
            if p.is_symlink():
                # No mode: a symlink's own permission bits are not
                # meaningful on Linux, and lstat's would be noise.
                out[rel] = ("l", os.readlink(p), 0)
            elif p.is_file():
                out[rel] = ("f", _hash_file(p), p.stat().st_mode & 0o777)
    return out


def scan_diff(work: Path, baseline: Path) -> tuple[list[str], list[str]]:
    """(writes, deletes) of work relative to baseline, by content+mode.

    Only regular files cross the wire, so the arithmetic is in terms of
    them — which is what keeps this in step with the overlay producer:

    - a regular file whose (content, mode) changed is a write;
    - a path that WAS a regular file and no longer is one — deleted, or
      replaced by a symlink — is a delete, because its content is gone
      from the merged view either way. That second case is the overlay's
      ``shadows`` rule, reached from the other direction;
    - a symlink is neither. It doesn't round-trip, but it is indexed, so
      it stops registering as an absence and being called a delete.
    """
    wi, bi = index_tree(work), index_tree(baseline)
    writes = sorted(
        p for p, ident in wi.items()
        if ident[0] == "f" and bi.get(p) != ident
    )
    deletes = sorted(
        p for p, ident in bi.items()
        # Paths the consumer never received can't be deleted from it.
        if ident[0] == "f" and (p not in wi or wi[p][0] != "f")
    )
    return writes, deletes


def make_tar(root: Path, paths: list[str]) -> bytes:
    # Plain tar, matching the host push writers: the wire is a local
    # socket, gzip only burns CPU (measured 4:1 on push at 200 MB).
    # Consumers extract with r:* so compressed producers stay valid.
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for rel in paths:
            tf.add(root / rel, arcname=rel, recursive=False)
    return buf.getvalue()


def extract_tar(data: bytes, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
        tf.extractall(dest, filter="data")


def sync_copy(src: Path, dst: Path) -> None:
    """Make dst an exact copy of src (used for reset and rebase).

    ``symlinks=True`` because the alternative FOLLOWS them, which broke
    two ways at once: a dangling link raised FileNotFoundError and took
    the whole rebase with it, and a live one landed in the baseline as a
    regular file — so the next scan saw a symlink in work, a file in
    baseline, and reported the path DELETED while it sat there intact.
    Copying the link itself keeps the two trees the same shape, which is
    all this function ever promised.
    """
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=True)


def clear_tree(root: Path) -> None:
    """Empty root without removing it."""
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
