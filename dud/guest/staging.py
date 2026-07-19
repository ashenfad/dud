"""Workspace staging strategies: scan-diff (rung 1) and overlayfs (VM).

The wire contract (a tar of changed/added files plus an explicit delete
list — see :mod:`dud.diffscan`) is producer-agnostic. This module holds
the two producers:

``ScanStage``
    Today's rung-1 mechanics: a pristine ``baseline/`` beside the
    mutable ``work/``, diffed by content hash. Works anywhere, costs
    O(tree) per diff and 2x the tree in storage.

``OverlayStage``
    The VM rungs' mechanics (stage 4-4): the workspace root *itself* is
    an overlayfs mount whose lowerdir is the pushed snapshot and whose
    upperdir *is* the diff. The staging trees live in a stash tmpfs
    OUTSIDE the mount (``/run/dud-stage``), so the agent-visible root
    contains exactly the workspace — no ``snap``/``upper`` internals to
    confuse (or corrupt: mutating a mounted overlay's backing dirs is
    kernel-documented undefined behavior, and guest code could reach
    them when they lived inside the root). Costs O(changes) per diff,
    1x tree + changes in RAM, and buys a *true* read-only workspace
    window for view execs (remount r/o — enforcement, not post-hoc
    detection). Requires being PID 1 on Linux with overlayfs in the
    kernel; anything short of that falls back to ``ScanStage``
    (``ping`` reports which one is live, so tests can refuse to pass
    on a silent fallback).

Overlay is mounted with ``redirect_dir=off,index=off,metacopy=off``:
those features optimize cases we don't have and would complicate
harvesting the upperdir (metadata-only copy-ups, rename redirects).
With them off, the upper contains exactly: copied-up regular files,
directories, 0:0 char-device whiteouts (deletions), and opaque-dir
xattrs (replaced directories) — which :func:`harvest` translates into
the wire's writes/deletes shape, expanding directory-level markers into
the per-file entries scan-diff would have produced (parity is load-
bearing: one corpus pins both producers).
"""

from __future__ import annotations

import ctypes
import errno
import filecmp
import os
import shutil
import stat
import sys
from contextlib import contextmanager
from pathlib import Path

from .. import diffscan
from ..diffscan import _IGNORE_DIRS, _IGNORE_SUFFIXES

MS_RDONLY = 1
MS_REMOUNT = 32
MNT_DETACH = 2

_libc = ctypes.CDLL(None, use_errno=True) if sys.platform == "linux" else None


class StageError(OSError):
    """A mount-layer operation failed; the stage is not usable."""


def _mount(source: str, target: Path, fstype: str, flags: int = 0,
           data: str | None = None) -> None:
    rc = _libc.mount(
        source.encode(), str(target).encode(), fstype.encode(),
        ctypes.c_ulong(flags), (data.encode() if data else None),
    )
    if rc != 0:
        e = ctypes.get_errno()
        raise StageError(e, f"mount {fstype} {target}: {os.strerror(e)}")


def _remount(target: Path, readonly: bool) -> None:
    flags = MS_REMOUNT | (MS_RDONLY if readonly else 0)
    rc = _libc.mount(b"overlay", str(target).encode(), b"overlay",
                     ctypes.c_ulong(flags), None)
    if rc != 0:
        e = ctypes.get_errno()
        raise StageError(e, f"remount {target}: {os.strerror(e)}")


def _umount(target: Path) -> None:
    rc = _libc.umount2(str(target).encode(), MNT_DETACH)
    if rc != 0:
        e = ctypes.get_errno()
        if e != errno.EINVAL:  # EINVAL = not mounted; fine
            raise StageError(e, f"umount {target}: {os.strerror(e)}")


# ---- harvest: upperdir -> wire shape ----------------------------------


def _is_whiteout(path: Path, st: os.stat_result) -> bool:
    return stat.S_ISCHR(st.st_mode) and st.st_rdev == 0


def _is_opaque(path: Path) -> bool:
    try:
        return os.getxattr(path, "trusted.overlay.opaque",
                           follow_symlinks=False) == b"y"
    except OSError:
        return False


def _files_under(root: Path, rel: str) -> list[str]:
    """Contract-relevant files below root, as rel-prefixed paths."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS]
        for name in filenames:
            if name.endswith(_IGNORE_SUFFIXES):
                continue
            p = Path(dirpath) / name
            if p.is_symlink() or not p.is_file():
                continue
            out.append(f"{rel}/{p.relative_to(root)}" if rel else
                       str(p.relative_to(root)))
    return out


def _same_content(a: Path, b: Path) -> bool:
    try:
        return (b.is_file() and not b.is_symlink()
                and filecmp.cmp(a, b, shallow=False))
    except OSError:
        return False


def harvest(
    upper: Path,
    snap: Path,
    is_whiteout=_is_whiteout,
    is_opaque=_is_opaque,
) -> tuple[list[str], list[str]]:
    """Translate an overlay upperdir into the wire's (writes, deletes).

    Parity rules (must match what scan-diff would report):
      - deletions expand to per-file entries (a whiteout or opaque
        marker over a directory becomes one delete per lower file);
      - a copied-up file whose content equals the snapshot's is NOT a
        write (overlay copies up on metadata-only touches);
      - ignore rules (__pycache__, *.pyc/pyo) apply to both sides;
      - symlinks and empty dirs don't round-trip (diffscan v0 parity) —
        but an upper symlink SHADOWING lower content still deletes it
        (scan-diff sees the lower file vanish from the merged index);
      - the walk assumes a quiescent tree: the supervisor is single-
        threaded and diff runs between execs. A background guest
        process writing mid-diff could tear the observation — exactly
        as it could under scan-diff, so parity holds even in the race.

    ``is_whiteout`` / ``is_opaque`` are injectable for host-side tests
    (real whiteouts need mknod; real opaque marks need trusted xattrs).
    """
    upper_files: dict[str, Path] = {}
    whiteouts: list[str] = []
    opaques: list[str] = []
    shadows: list[str] = []  # upper symlinks covering lower content

    def walk(rel_dir: str, in_opaque: bool) -> None:
        for entry in os.scandir(upper / rel_dir if rel_dir else upper):
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            st = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                if entry.name in _IGNORE_DIRS:
                    continue
                opq = not in_opaque and is_opaque(Path(entry.path))
                if opq:
                    opaques.append(rel)
                walk(rel, in_opaque or opq)
            elif is_whiteout(Path(entry.path), st):
                # Inside an opaque scope the lower is already hidden
                # wholesale; whiteouts there are redundant bookkeeping.
                if not in_opaque:
                    whiteouts.append(rel)
            elif stat.S_ISREG(st.st_mode):
                if not entry.name.endswith(_IGNORE_SUFFIXES):
                    upper_files[rel] = Path(entry.path)
            elif stat.S_ISLNK(st.st_mode):
                # Symlinks don't round-trip (v0), but one that covers
                # lower content still hides it from the merged index —
                # scan-diff would report the delete, so must we.
                # (Opaque scopes handle their own hidden lowers.)
                if not in_opaque:
                    shadows.append(rel)
            # other node types: skipped (v0 parity)

    walk("", False)

    writes: list[str] = []
    deletes: set[str] = set()
    for rel, p in sorted(upper_files.items()):
        lower = snap / rel
        if _same_content(p, lower):
            continue  # metadata-only copy-up: merged content unchanged
        writes.append(rel)
        if lower.is_dir():
            # dir -> file transition: the whiteout was consumed by the
            # replacing entry, but the lower dir's files are still gone.
            deletes.update(_files_under(lower, rel))
    for rel in whiteouts + shadows:
        target = snap / rel
        if target.is_dir() and not target.is_symlink():
            deletes.update(_files_under(target, rel))
        elif target.is_file() and not target.is_symlink():
            if not rel.endswith(_IGNORE_SUFFIXES):
                deletes.add(rel)
    for rel in opaques:
        target = snap / rel
        if target.is_dir():
            for frel in _files_under(target, rel):
                if frel not in upper_files:
                    deletes.add(frel)
        elif target.is_file():
            # file -> dir transition (mkdir over a whiteout goes opaque)
            if not rel.endswith(_IGNORE_SUFFIXES):
                deletes.add(rel)
    return writes, sorted(deletes)


# ---- stages ------------------------------------------------------------


class ScanStage:
    """Baseline-copy staging: works everywhere, O(tree) diffs."""

    kind = "scan"

    def __init__(self, root: Path):
        self.root = root
        self.work = root / "work"
        self.baseline = root / "baseline"
        self.work.mkdir(parents=True, exist_ok=True)
        self.baseline.mkdir(parents=True, exist_ok=True)

    def push(self, tar_bytes: bytes | None) -> None:
        diffscan.clear_tree(self.work)
        if tar_bytes:
            diffscan.extract_tar(tar_bytes, self.work)
        diffscan.sync_copy(self.work, self.baseline)

    def diff(self, rebase: bool) -> tuple[list[str], list[str], bytes]:
        writes, deletes = diffscan.scan_diff(self.work, self.baseline)
        tar = diffscan.make_tar(self.work, writes)
        if rebase:
            diffscan.sync_copy(self.work, self.baseline)
        return writes, deletes, tar

    def reset_stage(self) -> None:
        diffscan.sync_copy(self.baseline, self.work)

    def reset_guest(self, keep_tree: bool) -> None:
        if not keep_tree:
            diffscan.clear_tree(self.work)
            diffscan.clear_tree(self.baseline)

    @contextmanager
    def readonly(self):
        # Rung-1 documented gap: no fs isolation, no enforcement here.
        # Consumers keep their post-hoc diff check on this rung.
        yield


class OverlayStage:
    """Overlayfs staging: O(changes) diffs, real read-only windows.

    The merged mount lives AT ``root`` (the agent-visible workspace);
    the backing trees live in the ``stash`` tmpfs outside it. Keeping
    them out of the mount is load-bearing twice over: the workspace
    listing contains exactly the workspace (the ``/workspace/snap`` vs
    ``/workspace/work`` duplicate listings that confused agents are
    gone, and stray writes beside them no longer skirt the diff), and
    harvest reads ``upper``/``snap`` by path *while the overlay is
    mounted* — paths inside the mount would resolve through the merged
    view instead of the backing trees. (Root guest code can still
    reach the stash by its own path; that's OS territory on a
    disposable machine, not workspace state.)
    """

    kind = "overlay"

    def __init__(self, root: Path, stash: Path):
        self.root = root
        self.work = root  # the workspace root IS the merged mount
        self.stash = stash
        self.snap = stash / "snap"
        self.upper = stash / "upper"
        self.ovlwork = stash / "ovl-work"

    @classmethod
    def try_create(
        cls, root: Path, stash: Path = Path("/run/dud-stage")
    ) -> "OverlayStage | None":
        """Stand up the mount stack, or None (caller falls back to scan).

        Gated to Linux PID 1: that's the VM rung's signature, and it
        keeps a root-privileged subprocess rung on Linux from ever
        mounting over pieces of a real host. PID 1 also guarantees the
        default stash parent: init mounts a tmpfs on ``/run`` before
        the supervisor starts, so the stash is writable even on a
        read-only erofs root.
        """
        if sys.platform != "linux" or os.getpid() != 1:
            return None
        if os.environ.get("DUD_NO_OVERLAY"):
            return None
        st = cls(root, stash)
        try:
            root.mkdir(parents=True, exist_ok=True)
            stash.mkdir(parents=True, exist_ok=True)
            # tmpfs under the staging trees: ramfs (the initramfs root)
            # lacks the xattr support an overlay upperdir needs, and a
            # dedicated mount keeps the stash independent of how /run
            # itself is backed.
            _mount("tmpfs", stash, "tmpfs")
            for d in (st.snap, st.upper, st.ovlwork):
                d.mkdir()
            st._mount_overlay()
            return st
        except OSError as e:
            sys.stderr.write(f"[dud] overlay staging unavailable: {e}\n")
            for target in (st.work, stash):
                try:
                    _umount(target)
                except OSError:
                    pass
            return None

    def _mount_overlay(self) -> None:
        _mount(
            "overlay", self.work, "overlay",
            data=(
                f"lowerdir={self.snap},upperdir={self.upper},"
                f"workdir={self.ovlwork},"
                f"redirect_dir=off,index=off,metacopy=off,xino=off"
            ),
        )

    def _refresh(self, clear_snap: bool) -> None:
        """Remount with a fresh upper (and optionally a fresh lower).

        The lower/upper dirs must never be modified while the overlay
        is mounted (undefined behavior per kernel docs) — every
        mutation of them here happens between umount and mount.
        """
        _umount(self.work)
        for d in (self.upper, self.ovlwork) + ((self.snap,) if clear_snap else ()):
            shutil.rmtree(d, ignore_errors=True)
            d.mkdir()
        self._mount_overlay()

    def push(self, tar_bytes: bytes | None) -> None:
        _umount(self.work)
        for d in (self.snap, self.upper, self.ovlwork):
            shutil.rmtree(d, ignore_errors=True)
            d.mkdir()
        if tar_bytes:
            diffscan.extract_tar(tar_bytes, self.snap)
        self._mount_overlay()

    def diff(self, rebase: bool) -> tuple[list[str], list[str], bytes]:
        writes, deletes = harvest(self.upper, self.snap)
        tar = diffscan.make_tar(self.work, writes)  # merged view has truth
        if rebase:
            _umount(self.work)
            replay(self.upper, self.snap)
            for d in (self.upper, self.ovlwork):
                shutil.rmtree(d, ignore_errors=True)
                d.mkdir()
            self._mount_overlay()
        return writes, deletes, tar

    def reset_stage(self) -> None:
        self._refresh(clear_snap=False)

    def reset_guest(self, keep_tree: bool) -> None:
        if not keep_tree:
            self._refresh(clear_snap=True)

    @contextmanager
    def readonly(self):
        """A true read-only workspace window (view execs)."""
        try:
            _remount(self.work, readonly=True)
        except StageError as e:
            if e.errno == errno.EBUSY:
                # Fail loud but point at the likely culprit: EBUSY here
                # almost always means a stray guest process (from an
                # earlier shell()) holds a writable fd in the workspace.
                raise StageError(
                    e.errno,
                    f"{e.strerror}: cannot remount workspace read-only — "
                    f"a process may hold a writable fd in it",
                ) from e
            raise
        try:
            yield
        finally:
            _remount(self.work, readonly=False)


def replay(
    udir: Path,
    sdir: Path,
    is_whiteout=_is_whiteout,
    is_opaque=_is_opaque,
) -> None:
    """Fold an upperdir into the snapshot, exactly as overlay merges:
    whiteouts delete, opaque dirs replace, files copy up. Runs only
    while unmounted. This IS rebase — baseline := merged view.

    Predicates are injectable for host-side tests, same as harvest.
    """
    for entry in os.scandir(udir):
        spath = sdir / entry.name
        st = entry.stat(follow_symlinks=False)
        if stat.S_ISDIR(st.st_mode):
            if is_opaque(Path(entry.path)) or spath.is_file():
                _rmtree_or_unlink(spath)
            spath.mkdir(exist_ok=True)
            replay(Path(entry.path), spath, is_whiteout, is_opaque)
        elif is_whiteout(Path(entry.path), st):
            _rmtree_or_unlink(spath)
        elif stat.S_ISREG(st.st_mode):
            if spath.is_dir():
                _rmtree_or_unlink(spath)
            shutil.copy2(entry.path, spath)
        # symlinks/others: not part of the contract (v0)


def _rmtree_or_unlink(p: Path) -> None:
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p, ignore_errors=True)
    else:
        p.unlink(missing_ok=True)


def make_stage(root: Path):
    """The best staging this environment supports."""
    stage = OverlayStage.try_create(root)
    if stage is not None:
        return stage
    try:
        return ScanStage(root)
    except OSError as e:
        if e.errno == errno.EROFS:
            # An erofs root is only writable through the overlay tmpfs.
            # Landing here means overlay staging failed (its reason was
            # already logged to the console by try_create) — name the
            # situation instead of dying as PID 1 with a raw traceback.
            raise StageError(
                errno.EROFS,
                "workspace root is read-only (erofs root) and overlay "
                "staging is unavailable — see earlier console line for "
                "the overlay failure; the VM cannot stage without it",
            ) from e
        raise
