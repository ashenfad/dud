"""Layer pinned Debian packages into a rootfs — the wheels trick for
system tools.

Guests have no network, so anything beyond the base image must be
baked in at build time. For Python code that's prebuilt wheels
(:mod:`dud.images.wheels`); for system tools it's .debs: fetched from
``deb.debian.org`` by pinned URL + sha256 into the shared blob cache,
unpacked in pure Python (a ``.deb`` is an ``ar`` archive holding
``data.tar.{xz,gz}``), and folded into the FileSet with the same
merged-usr-aware machinery layers use.

This is deliberately NOT apt: no dependency resolution, no maintainer
scripts, no alternatives. Each pin is exactly one deb to unpack;
suitable only for leaf tools whose runtime deps already ship in the
base image (checked live once, then pinned) — or whose few missing
libs are themselves pinned alongside, same-source same-version. First
user: erofs-utils for the self-hosted image builder (bookworm deps
all in python:*-slim); second: e2fsprogs + its two sibling libs for
the scratch-volume bake.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ..errors import DudError
from . import rootfs
from .cpio import FileSet


class DebError(DudError):
    """A deb fetch or unpack failed: transport, digest, or format."""


@dataclass(frozen=True)
class DebSpec:
    """One pinned .deb: exact URL, exact digest, no resolution."""

    name: str
    version: str
    arch: str
    url: str
    sha256: str


# arm64-only for now: an amd64 erofs build fails loud in deb_spec.
# Add the bookworm amd64 erofs-utils pin before running on Intel.
DEBS: dict[tuple[str, str], DebSpec] = {
    ("erofs-utils", "arm64"): DebSpec(
        name="erofs-utils",
        version="1.5-1",
        arch="arm64",
        url=(
            "https://deb.debian.org/debian/pool/main/e/erofs-utils/"
            "erofs-utils_1.5-1_arm64.deb"
        ),
        sha256=(
            "e60b4d4c582a0f18b919adba9d955a789ffb96506a389414e69f795b1f73f6d6"
        ),
    ),
    # mke2fs for the scratch-volume bake (dud.images.scratch). Unlike
    # erofs-utils, e2fsprogs' library deps are NOT all in python:slim,
    # so its two sibling libs ride along (same source package, same
    # version — this is still pinning, not resolution).
    ("e2fsprogs", "arm64"): DebSpec(
        name="e2fsprogs",
        version="1.47.0-2+b2",
        arch="arm64",
        url=(
            "https://deb.debian.org/debian/pool/main/e/e2fsprogs/"
            "e2fsprogs_1.47.0-2+b2_arm64.deb"
        ),
        sha256=(
            "9842c31d32c897e3414168c4fab34cddd1633adacbc7858896e7a797d1be1b24"
        ),
    ),
    ("libext2fs2", "arm64"): DebSpec(
        name="libext2fs2",
        version="1.47.0-2+b2",
        arch="arm64",
        url=(
            "https://deb.debian.org/debian/pool/main/e/e2fsprogs/"
            "libext2fs2_1.47.0-2+b2_arm64.deb"
        ),
        sha256=(
            "c1d2551a6238d6a1c64601a0d68183573ca2b5dbd213068d50eaa0747ac1b406"
        ),
    ),
    ("libcom-err2", "arm64"): DebSpec(
        name="libcom-err2",
        version="1.47.0-2+b2",
        arch="arm64",
        url=(
            "https://deb.debian.org/debian/pool/main/e/e2fsprogs/"
            "libcom-err2_1.47.0-2+b2_arm64.deb"
        ),
        sha256=(
            "36c15f933a965b50f4c9558d792d9556934c84e532703935d8ae7e69dd3fa863"
        ),
    ),
}


def deb_spec(name: str, arch: str) -> DebSpec:
    spec = DEBS.get((name, arch))
    if spec is None:
        known = sorted(f"{n} ({a})" for n, a in DEBS)
        raise DebError(f"no pinned deb {name!r} for {arch}; known: {known}")
    return spec


def fetch_deb(spec: DebSpec, home: Path) -> Path:
    """Download into the content-addressed blob cache (shared with
    registry blobs — a deb IS its digest)."""
    dest = home / "blobs" / "sha256" / spec.sha256
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    h = hashlib.sha256()
    try:
        with urllib.request.urlopen(spec.url, timeout=120) as r, \
                open(tmp, "wb") as f:
            while chunk := r.read(1 << 20):
                h.update(chunk)
                f.write(chunk)
    except OSError as e:
        raise DebError(f"fetch {spec.url} failed: {e}") from e
    if h.hexdigest() != spec.sha256:
        tmp.unlink(missing_ok=True)
        raise DebError(
            f"{spec.name}: digest mismatch (got {h.hexdigest()}, "
            f"want {spec.sha256})"
        )
    tmp.rename(dest)
    return dest


# ---- ar / deb format ---------------------------------------------------

_AR_MAGIC = b"!<arch>\n"


def _ar_members(data: bytes):
    """Yield (name, payload) from an ar archive (the .deb container)."""
    if not data.startswith(_AR_MAGIC):
        raise DebError("not an ar archive (bad magic)")
    off = len(_AR_MAGIC)
    while off + 60 <= len(data):
        header = data[off:off + 60]
        if header[58:60] != b"`\n":
            raise DebError(f"corrupt ar member header at offset {off}")
        name = header[0:16].decode().strip().rstrip("/")
        size = int(header[48:58].decode().strip())
        payload = data[off + 60:off + 60 + size]
        if len(payload) != size:
            raise DebError(f"truncated ar member {name!r}")
        yield name, payload
        off += 60 + size + (size % 2)  # members are 2-byte aligned


def _data_tar(deb_path: Path) -> tarfile.TarFile:
    """The deb's data.tar member, opened for streaming (xz/gz/plain)."""
    for name, payload in _ar_members(deb_path.read_bytes()):
        if name.startswith("data.tar"):
            if name.endswith((".zst", ".lzma")):
                raise DebError(
                    f"unsupported data.tar compression in {deb_path.name}: "
                    f"{name} (xz/gz supported)"
                )
            return tarfile.open(fileobj=io.BytesIO(payload), mode="r:*")
    raise DebError(f"{deb_path.name}: no data.tar member")


def add_deb_tree(fileset: FileSet, deb_path: Path) -> None:
    """Fold a deb's payload into the fileset, layer-style: root-owned,
    merged-usr symlink-parent aware, same entry semantics as image
    layers (maintainer scripts intentionally never run)."""
    with _data_tar(deb_path) as tf:
        for m in tf:
            path = rootfs._safe(m.name)
            if path is None:
                continue
            resolved = rootfs._resolve_parents(fileset, fileset.nodes, path)
            if resolved is None:
                continue
            rootfs._collect_entry(fileset, fileset.nodes, tf, m, resolved)
