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
from dataclasses import dataclass
from pathlib import Path

from . import registry, rootfs

# Bump when the flatten/inject/cpio logic changes shape in a way that
# should invalidate cached rootfs artifacts.
PIPELINE_VERSION = 1


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

    @property
    def meta_path(self) -> Path:
        return self.rootfs_path.with_name("meta.json")


def _dud_code_hash() -> str:
    h = hashlib.sha256()
    for rel, data in sorted(rootfs._dud_package_files().items()):
        h.update(rel.encode())
        h.update(hashlib.sha256(data).digest())
    return h.hexdigest()


def _spec_hash(digest: str, workspace: str) -> str:
    h = hashlib.sha256()
    h.update(f"v{PIPELINE_VERSION}\0".encode())
    h.update(f"{digest}\0".encode())
    h.update(f"{workspace}\0".encode())
    h.update(_dud_code_hash().encode())
    return h.hexdigest()[:24]


def build(
    ref: str,
    arch: str | None = None,
    workspace: str = "/workspace",
    home: Path | None = None,
    force: bool = False,
) -> RootfsBuild:
    """Produce (or reuse) a rootfs initramfs for ``ref``."""
    home = home or dud_home()
    reg = registry.Registry(home)
    image = reg.pull(ref, arch=arch)
    spec = _spec_hash(image.digest, workspace)

    out_dir = home / "images" / spec
    rootfs_path = out_dir / "rootfs.cpio.gz"
    result = RootfsBuild(
        spec=spec,
        ref=str(image.ref),
        digest=image.digest,
        rootfs_path=rootfs_path,
        workspace=workspace,
        env=image.env,
        workdir=image.workdir,
    )

    if rootfs_path.exists() and not force:
        return result

    out_dir.mkdir(parents=True, exist_ok=True)
    fileset = rootfs.build_fileset(image, workspace=workspace)
    from .cpio import build_cpio_gz

    data = build_cpio_gz(fileset)
    tmp = rootfs_path.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.rename(rootfs_path)
    result.meta_path.write_text(json.dumps({
        "spec": spec,
        "ref": result.ref,
        "digest": result.digest,
        "workspace": workspace,
        "env": result.env,
        "workdir": result.workdir,
        "pipeline_version": PIPELINE_VERSION,
        "entries": len(fileset.nodes),
        "size": len(data),
    }, indent=2))
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="dud.images.builder")
    ap.add_argument("ref", help="image reference, e.g. python:3.12-slim")
    ap.add_argument("--arch", default=None, help="linux arch (default: host)")
    ap.add_argument("--workspace", default="/workspace")
    ap.add_argument("--force", action="store_true", help="rebuild even if cached")
    args = ap.parse_args(argv)

    r = build(args.ref, arch=args.arch, workspace=args.workspace, force=args.force)
    size_mb = r.rootfs_path.stat().st_size / 1e6
    print(f"ref     {r.ref}")
    print(f"digest  {r.digest}")
    print(f"spec    {r.spec}")
    print(f"rootfs  {r.rootfs_path}  ({size_mb:.1f} MB)")
    print(f"env     {len(r.env)} vars; workdir {r.workdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
