"""Blank scratch volumes: disk as a cache for computation.

The scratch plane (DESIGN.md "The scratch plane") gives a VM a
writable ext4 volume mounted at ``/tmp`` whose contents are *cache*,
not state: droppable at any moment, never part of diffs, commits,
restores, or forks. This module bakes the blank master image the
plane starts from.

There is no mkfs.ext4 on macOS, so the bake is self-hosted like the
erofs build: a builder VM with pinned e2fsprogs debs formats a sparse
file and returns it zero-compressed; the host re-sparsifies while
unpacking. Hosts with a native mke2fs (Linux; brew's keg-only
e2fsprogs) skip the VM detour entirely. Cached under ``~/.dud/scratch``
by size — the bake runs once per size class, ever.

ext4 rather than erofs because this is the one place writability is
the point; the journal is what lets a crashed VM's volume mount clean
(in-kernel replay) with no userspace fsck in the guest.
"""

from __future__ import annotations

import gzip
import io
import os
import shutil
import subprocess
import threading
from pathlib import Path

from ..errors import DudError
from . import dud_home

_CHUNK = 1 << 20
_SCRATCH_DEBS = ["e2fsprogs", "libext2fs2", "libcom-err2"]
_BUILDER_IMAGE = "python:3.12-slim"

# brew installs e2fsprogs keg-only (never on PATH).
_BREW_MKE2FS = (
    "/opt/homebrew/opt/e2fsprogs/sbin/mke2fs",
    "/usr/local/opt/e2fsprogs/sbin/mke2fs",
)


class ScratchError(DudError):
    """A scratch volume bake failed."""


def _host_mke2fs() -> str | None:
    for cand in (shutil.which("mke2fs"), shutil.which("mkfs.ext4"),
                 *_BREW_MKE2FS):
        if cand and Path(cand).exists():
            return cand
    return None


def blank_ext4(size_mib: int = 4096, home: str | Path | None = None,
               arch: str | None = None) -> Path:
    """The cached blank master for ``size_mib``; bake it if absent.

    The file is sparse — host disk holds only the fs metadata (a few
    MB) until a guest writes real cache into a clone of it.
    """
    home = Path(home) if home else dud_home()
    dest = home / "scratch" / f"blank-{size_mib}m.ext4"
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Unique temp per baker: two processes racing the first bake must
    # not interleave mke2fs runs on one file (each rename below is a
    # complete image; last-wins is fine — they're identical blanks).
    tmp = dest.with_suffix(f".part.{os.getpid()}.{threading.get_ident()}")
    try:
        tool = _host_mke2fs()
        if tool:
            _bake_host(tool, tmp, size_mib)
        else:
            _bake_vm(tmp, size_mib, home, arch)
        tmp.rename(dest)
    finally:
        tmp.unlink(missing_ok=True)  # failed bake leaves no residue
    return dest


def _bake_host(tool: str, tmp: Path, size_mib: int) -> None:
    with open(tmp, "wb") as f:
        f.truncate(size_mib << 20)
    r = subprocess.run(
        [tool, "-t", "ext4", "-F", "-q", "-L", "dud-scratch", str(tmp)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise ScratchError(f"{tool} failed: {r.stderr or r.stdout}")


def _bake_vm(tmp: Path, size_mib: int, home: Path, arch: str | None) -> None:
    """Self-hosted mke2fs. The guest gzips the (almost entirely zero)
    image so only megabytes cross the wire; sparseness is restored
    host-side."""
    import platform

    if platform.system() != "Darwin":  # pragma: no cover - future rungs
        raise ScratchError(
            "no host mke2fs and the builder VM path requires macOS/vfkit"
        )
    from ..backends.vfkit import VfkitSession  # lazy: circular import

    session = VfkitSession(image=_BUILDER_IMAGE, arch=arch, home=home,
                           debs=_SCRATCH_DEBS, medium="initramfs")
    try:
        r = session.shell(
            f"truncate -s {size_mib}M scratch.img"
            f" && mke2fs -t ext4 -F -q -L dud-scratch scratch.img"
            f" && gzip -1 scratch.img",
            timeout=600.0,
        )
        if r.exit_code != 0:
            raise ScratchError(f"scratch bake failed:\n{r.transcript}")
        gz = session.diff().writes["scratch.img.gz"]
    finally:
        session.close()
    _unpack_sparse(gz, tmp, size_mib << 20)


def scratch_master(key: str, size_mib: int = 4096,
                   home: str | Path | None = None) -> Path:
    """The per-key writable master for ``key`` (a published-app token,
    a commit hash, ...), created as a CoW clone of the blank on first
    use. Pass the returned path as ``VfkitSession(scratch=...)`` —
    each boot clones it again, and clean parks promote back into it.

    Keys are sanitized for the filesystem with a short content hash
    appended, so distinct keys can never collide. All masters live
    under ``~/.dud/scratch/keys/`` — the natural seam for future
    eviction/GC.
    """
    if not key:
        raise ScratchError("scratch_master requires a non-empty key")
    import hashlib
    import re as _re

    safe = _re.sub(r"[^A-Za-z0-9._-]", "_", key)[:80]
    digest = hashlib.sha256(key.encode()).hexdigest()[:8]
    home = Path(home) if home else dud_home()
    dest = home / "scratch" / "keys" / f"{safe}-{digest}" / f"master-{size_mib}m.ext4"
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    blank = blank_ext4(size_mib, home=home)
    tmp = dest.with_suffix(f".part.{os.getpid()}.{threading.get_ident()}")
    try:
        _clone_or_copy(blank, tmp)
        tmp.rename(dest)
    finally:
        tmp.unlink(missing_ok=True)
    return dest


def _clone_or_copy(src: Path, dest: Path) -> None:
    """APFS clonefile when possible (instant, CoW), real copy where
    the flag or filesystem doesn't support it. (The Linux twin is
    ``cp --reflink=auto`` — one branch here when the firecracker rung
    lands.)"""
    r = subprocess.run(["cp", "-c", str(src), str(dest)],
                       capture_output=True)
    if r.returncode != 0:
        shutil.copyfile(src, dest)


def _unpack_sparse(gz: bytes, dest: Path, size: int) -> None:
    """gunzip to ``dest``, seeking over zero chunks so the image lands
    as sparse as mke2fs made it."""
    with gzip.GzipFile(fileobj=io.BytesIO(gz)) as f, open(dest, "wb") as out:
        while chunk := f.read(_CHUNK):
            if chunk.count(0) == len(chunk):
                out.seek(len(chunk), 1)
            else:
                out.write(chunk)
        out.truncate(size)
