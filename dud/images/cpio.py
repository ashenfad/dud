"""Emit a ``newc`` cpio initramfs in memory.

The Linux kernel unpacks an initramfs from a ``newc``-format cpio archive
(the format ``gen_init_cpio`` produces). Building it ourselves — rather
than extracting to a real filesystem and shelling out to ``cpio`` — lets
us force ``uid/gid 0`` and exact mode bits regardless of the host, dodge
macOS case-insensitivity collisions on a Debian tree, and stay
dependency-free and deterministic (fixed mtime, ascending inodes).

A ``FileSet`` is the merged view of a flattened image plus injected
files; ``build_cpio_gz`` serializes it. Entries are emitted parents
first (kernel requirement) by sorting on path depth then name.
"""

from __future__ import annotations

import gzip
import io
from dataclasses import dataclass, field

S_IFDIR = 0o040000
S_IFREG = 0o100000
S_IFLNK = 0o120000


@dataclass
class Node:
    """One filesystem entry destined for the initramfs."""

    mode: int  # type bits | permission bits
    data: bytes = b""  # file contents, or symlink target for S_IFLNK
    uid: int = 0
    gid: int = 0
    mtime: int = 0


@dataclass
class FileSet:
    """Path -> Node, normalized to forward slashes without a leading '/'."""

    nodes: dict[str, Node] = field(default_factory=dict)

    @staticmethod
    def _norm(path: str) -> str:
        return path.strip("/").replace("//", "/")

    def add_dir(self, path: str, perm: int = 0o755) -> None:
        p = self._norm(path)
        if p:
            self.nodes[p] = Node(mode=S_IFDIR | perm)

    def add_file(self, path: str, data: bytes, perm: int = 0o644) -> None:
        self.nodes[self._norm(path)] = Node(mode=S_IFREG | perm, data=data)

    def add_symlink(self, path: str, target: str) -> None:
        self.nodes[self._norm(path)] = Node(
            mode=S_IFLNK | 0o777, data=target.encode()
        )

    def remove_subtree(self, path: str) -> None:
        """Drop ``path`` and everything beneath it (whiteout semantics)."""
        p = self._norm(path)
        prefix = p + "/"
        for key in [k for k in self.nodes if k == p or k.startswith(prefix)]:
            del self.nodes[key]

    def ensure_parents(self) -> None:
        """Synthesize any missing ancestor directories as 0755."""
        needed: set[str] = set()
        for path in self.nodes:
            parts = path.split("/")
            for i in range(1, len(parts)):
                needed.add("/".join(parts[:i]))
        for d in needed:
            if d not in self.nodes:
                self.nodes[d] = Node(mode=S_IFDIR | 0o755)
            elif not self.nodes[d].mode & S_IFDIR:
                # A file shadows a needed dir path — the dir wins (a later
                # layer turned a file into a directory).
                self.nodes[d] = Node(mode=S_IFDIR | 0o755)


def _field(value: int) -> bytes:
    return b"%08X" % (value & 0xFFFFFFFF)


def _pad4(buf: io.BytesIO) -> None:
    if buf.tell() % 4:
        buf.write(b"\x00" * (4 - buf.tell() % 4))


def _entry(buf: io.BytesIO, ino: int, name: str, node: Node) -> None:
    name_bytes = name.encode() + b"\x00"
    is_dir = bool(node.mode & S_IFDIR)
    header = (
        b"070701"
        + _field(ino)
        + _field(node.mode)
        + _field(node.uid)
        + _field(node.gid)
        + _field(2 if is_dir else 1)  # nlink
        + _field(node.mtime)
        + _field(len(node.data))
        + _field(0) + _field(0)       # devmajor, devminor
        + _field(0) + _field(0)       # rdevmajor, rdevminor
        + _field(len(name_bytes))
        + _field(0)                   # check (unused for newc)
    )
    buf.write(header)
    buf.write(name_bytes)
    _pad4(buf)
    buf.write(node.data)
    _pad4(buf)


def build_cpio(fileset: FileSet) -> bytes:
    """Serialize a FileSet to an uncompressed newc cpio archive."""
    fileset.ensure_parents()
    buf = io.BytesIO()
    # Parents before children: sort by depth, then lexicographically.
    ordered = sorted(fileset.nodes.items(), key=lambda kv: (kv[0].count("/"), kv[0]))
    for ino, (path, node) in enumerate(ordered, start=1):
        _entry(buf, ino, path, node)
    # Trailer marks end of archive.
    _entry(buf, 0, "TRAILER!!!", Node(mode=0))
    _pad4(buf)
    return buf.getvalue()


def build_cpio_gz(fileset: FileSet) -> bytes:
    """Serialize and gzip a FileSet (an initramfs image)."""
    raw = build_cpio(fileset)
    out = io.BytesIO()
    # mtime=0 for reproducible output.
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
        gz.write(raw)
    return out.getvalue()
