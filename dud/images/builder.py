"""Build a bootable dud rootfs from an image ref, cached by spec hash.

Ties the pipeline together: pull (``registry``) -> flatten + inject
(``rootfs``) -> serialize (``cpio``), memoized under ``~/.dud``. The
spec hash folds in the image's manifest digest, the dud guest code, the
workspace path, and the pipeline version, so a change to any of them
mints a fresh artifact while an unchanged spec is a no-op re-read.

The kernel is *not* built here: it is a bundled dud asset (an
uncompressed arm64 ``Image`` with virtio + vsock), reused across every
rootfs. This module owns only the rootfs half.

CLI:  python -m dud.images.builder python:3.12-slim [--arch arm64]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import registry, rootfs

# Bump when the flatten/inject/cpio logic changes shape in a way that
# should invalidate cached rootfs artifacts.
PIPELINE_VERSION = 1

# Rootfs media the backend can boot. The medium is folded into the spec
# hash so a threshold change can never serve a wrong-medium artifact, and
# stamped into meta.json so the backend picks the vfkit device flags
# without guessing. Only ``initramfs`` is built today; ``ext4`` (large
# images, demand-paged from a virtio-blk disk) is the additive scale path.
_MEDIUM_FILENAME = {
    "initramfs": "rootfs.cpio.gz",
    "ext4": "rootfs.ext4",
}


def dud_home() -> Path:
    return Path(os.environ.get("DUD_HOME", str(Path.home() / ".dud")))


@dataclass
class RootfsBuild:
    """A materialized rootfs and the metadata a backend needs to boot it."""

    spec: str
    ref: str
    digest: str
    rootfs_path: Path
    workspace: str
    env: list[str]
    workdir: str
    medium: str = "initramfs"

    @property
    def meta_path(self) -> Path:
        return self.rootfs_path.with_name("meta.json")


def _dud_code_hash() -> str:
    h = hashlib.sha256()
    for rel, data in sorted(rootfs._dud_package_files().items()):
        h.update(rel.encode())
        h.update(hashlib.sha256(data).digest())
    return h.hexdigest()


def _spec_hash(
    digest: str, workspace: str, medium: str, packages: tuple[str, ...]
) -> str:
    h = hashlib.sha256()
    h.update(f"v{PIPELINE_VERSION}\0".encode())
    h.update(f"{digest}\0".encode())
    h.update(f"{workspace}\0".encode())
    h.update(f"{medium}\0".encode())
    h.update(("\0".join(packages) + "\0").encode())
    h.update(_dud_code_hash().encode())
    return h.hexdigest()[:24]


def build(
    ref: str,
    arch: str | None = None,
    workspace: str = "/workspace",
    home: Path | None = None,
    force: bool = False,
    medium: str = "initramfs",
    packages: list[str] | None = None,
) -> RootfsBuild:
    """Produce (or reuse) a rootfs for ``ref`` in the requested medium.

    ``packages`` layers prebuilt guest-arch wheels into the image's
    ``site-packages`` (see :mod:`dud.images.wheels`) — e.g. the data
    stack a workspace needs but ``python:slim`` doesn't ship.
    """
    if medium not in _MEDIUM_FILENAME:
        raise ValueError(f"unknown rootfs medium {medium!r}")
    pkgs = tuple(sorted(packages or ()))
    home = home or dud_home()
    reg = registry.Registry(home)
    image = reg.pull(ref, arch=arch)
    resolved_arch = arch or registry._host_arch()
    spec = _spec_hash(image.digest, workspace, medium, pkgs)

    out_dir = home / "images" / spec
    rootfs_path = out_dir / _MEDIUM_FILENAME[medium]
    result = RootfsBuild(
        spec=spec,
        ref=str(image.ref),
        digest=image.digest,
        rootfs_path=rootfs_path,
        workspace=workspace,
        env=image.env,
        workdir=image.workdir,
        medium=medium,
    )

    if rootfs_path.exists() and not force:
        return result

    out_dir.mkdir(parents=True, exist_ok=True)
    fileset = rootfs.build_fileset(image, workspace=workspace)
    if pkgs:
        _layer_packages(fileset, list(pkgs), resolved_arch)
    data = _serialize(fileset, medium)
    tmp = rootfs_path.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.rename(rootfs_path)
    result.meta_path.write_text(json.dumps({
        "spec": spec,
        "ref": result.ref,
        "digest": result.digest,
        "workspace": workspace,
        "medium": medium,
        "artifact": _MEDIUM_FILENAME[medium],
        "packages": list(pkgs),
        "env": result.env,
        "workdir": result.workdir,
        "pipeline_version": PIPELINE_VERSION,
        "entries": len(fileset.nodes),
        "size": len(data),
    }, indent=2))
    return result


def _layer_packages(fileset, packages: list[str], arch: str) -> None:
    """Resolve guest-arch wheels and fold them into site-packages."""
    from . import wheels

    site = rootfs._site_packages(fileset)
    py = wheels.python_version_from_site(site)
    with tempfile.TemporaryDirectory(prefix="dud-wheels-") as td:
        wheels.resolve_wheels(packages, Path(td), arch, py)
        wheels.add_target_tree(fileset, Path(td), site)


def _serialize(fileset, medium: str) -> bytes:
    if medium == "initramfs":
        from .cpio import build_cpio_gz

        return build_cpio_gz(fileset)
    # ext4 needs a userspace image builder (e2fsprogs mke2fs -d, or a Linux
    # helper VM) and is the scale path for large images — not wired yet.
    raise NotImplementedError(
        f"medium {medium!r} is not implemented; only 'initramfs' builds today"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="dud.images.builder")
    ap.add_argument("ref", help="image reference, e.g. python:3.12-slim")
    ap.add_argument("--arch", default=None, help="linux arch (default: host)")
    ap.add_argument("--workspace", default="/workspace")
    ap.add_argument("--medium", default="initramfs",
                    choices=sorted(_MEDIUM_FILENAME), help="rootfs medium")
    ap.add_argument("--package", action="append", default=[], metavar="PKG",
                    help="pip package to layer in (repeatable)")
    ap.add_argument("--force", action="store_true", help="rebuild even if cached")
    args = ap.parse_args(argv)

    r = build(args.ref, arch=args.arch, workspace=args.workspace,
              force=args.force, medium=args.medium, packages=args.package)
    size_mb = r.rootfs_path.stat().st_size / 1e6
    print(f"ref     {r.ref}")
    print(f"digest  {r.digest}")
    print(f"spec    {r.spec}")
    print(f"rootfs  {r.rootfs_path}  ({size_mb:.1f} MB, {r.medium})")
    print(f"env     {len(r.env)} vars; workdir {r.workdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
