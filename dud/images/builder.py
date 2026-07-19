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
# v2: layered packages ship baked hash-based .pyc (imports were
#     recompiling pandas per exec: ~1s per view GET); debs marker
#     folded unconditionally into the spec hash.
PIPELINE_VERSION = 2

# Rootfs media the backend can boot. The medium is folded into the spec
# hash so a threshold change can never serve a wrong-medium artifact, and
# stamped into meta.json so the backend picks the vfkit device flags
# without guessing. Only ``initramfs`` is built today; ``erofs`` (large
# images, demand-paged read-only from a virtio-blk disk) is the additive
# scale path, built by the self-hosted builder (a dud VM with erofs-utils).
_MEDIUM_FILENAME = {
    "initramfs": "rootfs.cpio.gz",
    "erofs": "rootfs.erofs",
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
    digest: str, workspace: str, medium: str, packages: tuple[str, ...],
    debs: tuple[str, ...] = (),
) -> str:
    h = hashlib.sha256()
    h.update(f"v{PIPELINE_VERSION}\0".encode())
    h.update(f"{digest}\0".encode())
    h.update(f"{workspace}\0".encode())
    h.update(f"{medium}\0".encode())
    h.update(("\0".join(packages) + "\0").encode())
    h.update(("\0".join(debs) + "\0").encode())
    h.update(_dud_code_hash().encode())
    return h.hexdigest()[:24]


# medium="auto" picks per image: small pure-slim images stay initramfs
# (zero moving parts); anything with layered packages or a big base
# goes erofs (demand-paged, page-cache shared). The threshold reads the
# pulled layer blobs (compressed, on disk) — cheap and deterministic
# per digest, so the choice is stable for a given spec.
_AUTO_EROFS_LAYER_BYTES = 100 * 1024 * 1024


def _resolve_medium(medium: str, image, packages: tuple[str, ...]) -> str:
    if medium != "auto":
        return medium
    layer_bytes = sum(p.stat().st_size for p in image.layer_paths)
    if packages or layer_bytes > _AUTO_EROFS_LAYER_BYTES:
        return "erofs"
    return "initramfs"


def build(
    ref: str,
    arch: str | None = None,
    workspace: str = "/workspace",
    home: Path | None = None,
    force: bool = False,
    medium: str = "initramfs",
    packages: list[str] | None = None,
    debs: list[str] | None = None,
) -> RootfsBuild:
    """Produce (or reuse) a rootfs for ``ref`` in the requested medium.

    ``packages`` layers prebuilt guest-arch wheels into the image's
    ``site-packages`` (see :mod:`dud.images.wheels`) — e.g. the data
    stack a workspace needs but ``python:slim`` doesn't ship.
    ``debs`` layers pinned system packages the same way (see
    :mod:`dud.images.debs`) — e.g. erofs-utils for a builder VM.
    ``medium="auto"`` resolves per image (see ``_resolve_medium``);
    the RESOLVED medium is what enters the spec hash and meta.json.
    """
    if medium not in _MEDIUM_FILENAME and medium != "auto":
        raise ValueError(f"unknown rootfs medium {medium!r}")
    pkgs = tuple(sorted(packages or ()))
    deb_names = tuple(sorted(debs or ()))
    home = home or dud_home()
    reg = registry.Registry(home)
    image = reg.pull(ref, arch=arch)
    resolved_arch = arch or registry._host_arch()
    medium = _resolve_medium(medium, image, pkgs)
    spec = _spec_hash(image.digest, workspace, medium, pkgs, deb_names)

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
    if deb_names:
        _layer_debs(fileset, list(deb_names), resolved_arch, home)
    data = _serialize(fileset, medium, home, resolved_arch)
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
        "debs": list(deb_names),
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
        _bytecompile(Path(td), py)
        wheels.add_target_tree(fileset, Path(td), site)


def _bytecompile(root: Path, py: str) -> None:
    """Bake .pyc files into the layered tree.

    Wheels unpacked by resolve_wheels carry no bytecode, and the guest
    can't durably write any (script model resets; erofs roots are
    immutable) — so without this, EVERY exec recompiles its imports
    from source (measured: ~1s per view GET, almost entirely pandas
    recompilation). Hash-based UNCHECKED pycs are the right kind for a
    baked image: sources never change underneath them, and they dodge
    both our deterministic zero mtimes and the validation stat.

    Bytecode is minor-version-scoped, so only bake when the host
    interpreter matches the guest's (python:3.12-* guest, 3.12 host —
    the studio norm); a mismatch silently skips, costing speed only.
    """
    import compileall
    import py_compile

    host_py = f"{sys.version_info.major}.{sys.version_info.minor}"
    if host_py != py:
        return
    # workers=1 = sequential in-process: parallel workers use
    # multiprocessing spawn, which re-imports the caller's __main__ —
    # a landmine for embedded callers (REPLs, heredocs). One-time cost
    # per image build; a few seconds is fine.
    compileall.compile_dir(
        str(root), quiet=2, workers=1,
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
    )


def _layer_debs(fileset, names: list[str], arch: str, home: Path) -> None:
    """Fetch pinned debs and fold their payloads into the tree."""
    from . import debs as debs_mod

    for name in names:
        spec = debs_mod.deb_spec(name, arch)
        debs_mod.add_deb_tree(fileset, debs_mod.fetch_deb(spec, home))


def _serialize(fileset, medium: str, home: Path, arch: str) -> bytes:
    if medium == "initramfs":
        from .cpio import build_cpio_gz

        return build_cpio_gz(fileset)
    if medium == "erofs":
        return _build_erofs(fileset, home, arch)
    raise NotImplementedError(f"medium {medium!r} is not implemented")


# The erofs builder VM is itself a cached initramfs spec (slim +
# erofs-utils via the pinned-deb layer) — built once ever, booted per
# erofs build. No recursion: the builder never needs an erofs root.
_BUILDER_IMAGE = "python:3.12-slim"


def _build_erofs(fileset, home: Path, arch: str) -> bytes:
    """mkfs.erofs directly where the host has it (Linux), inside a
    builder VM where it doesn't (macOS) — same flags, same artifact."""
    import platform
    import shutil

    tool = shutil.which("mkfs.erofs")
    if tool:
        return _build_erofs_host(tool, fileset)
    if platform.system() != "Darwin":  # pragma: no cover - odd hosts
        raise NotImplementedError(
            "no mkfs.erofs on PATH and the builder-VM path requires macOS"
        )
    from ..backends.vfkit import VfkitSession  # lazy: circular import

    tar = _fileset_tar(fileset, prefix="src")
    # Content lands on the guest's workspace tmpfs (lower) and the
    # image accumulates in the upper: size the VM for both plus slack.
    mem = max(2048, (len(tar) >> 20) * 3 + 1024)
    session = VfkitSession(
        image=_BUILDER_IMAGE, arch=arch, home=home,
        debs=["erofs-utils"], memory_mib=mem,
        # Structural, not heuristic: the erofs builder must never
        # itself resolve to an erofs root (unbounded recursion).
        medium="initramfs",
    )
    try:
        session.push_tree(tar)
        r = session.shell(
            "mkfs.erofs -zlz4 -T0 rootfs.erofs src/", timeout=600.0
        )
        if r.exit_code != 0:
            raise RuntimeError(f"mkfs.erofs failed:\n{r.transcript}")
        return session.diff().writes["rootfs.erofs"]
    finally:
        session.close()


def _build_erofs_host(tool: str, fileset) -> bytes:
    """Native mkfs.erofs: extract the fileset tar (same tar the VM
    path pushes — same ``data``-filter semantics) and pack. --all-root
    restores the root ownership that user-mode extraction loses."""
    import io
    import subprocess
    import tarfile

    with tempfile.TemporaryDirectory() as td:
        with tarfile.open(
            fileobj=io.BytesIO(_fileset_tar(fileset, prefix="src")), mode="r:"
        ) as tf:
            tf.extractall(td, filter="data")
        out = Path(td) / "rootfs.erofs"
        r = subprocess.run(
            [tool, "--all-root", "-zlz4", "-T0", str(out),
             str(Path(td) / "src")],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(
                f"mkfs.erofs failed:\n{r.stderr or r.stdout}"
            )
        return out.read_bytes()


def _fileset_tar(fileset, prefix: str) -> bytes:
    """Serialize a FileSet as a tar under ``prefix/``, extraction-safe.

    Absolute symlink targets are rewritten relative: at runtime inside
    the image they resolve identically, and it keeps the tar acceptable
    to tarfile's ``data`` filter (which rejects absolute link targets)
    on the builder-VM push path.

    Known divergence from the cpio path: that same ``data`` filter
    strips setuid/setgid/sticky bits at extraction, so an erofs image's
    ``su``-style binaries lose them while an initramfs keeps exact
    modes. Irrelevant while guests run everything as root; revisit if a
    non-root guest model ever appears.
    """
    import io
    import posixpath
    import tarfile

    from .cpio import is_dir, is_symlink

    fileset.ensure_parents()
    buf = io.BytesIO()
    ordered = sorted(fileset.nodes.items(),
                     key=lambda kv: (kv[0].count("/"), kv[0]))
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for path, node in ordered:
            info = tarfile.TarInfo(f"{prefix}/{path}")
            info.mode = node.mode & 0o7777
            if is_dir(node.mode):
                info.type = tarfile.DIRTYPE
                tf.addfile(info)
            elif is_symlink(node.mode):
                target = node.data.decode()
                if target.startswith("/"):
                    target = posixpath.relpath(
                        target.lstrip("/"), posixpath.dirname(path) or "."
                    )
                info.type = tarfile.SYMTYPE
                info.linkname = target
                tf.addfile(info)
            else:
                info.size = len(node.data)
                tf.addfile(info, io.BytesIO(node.data))
    return buf.getvalue()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="dud.images.builder")
    ap.add_argument("ref", help="image reference, e.g. python:3.12-slim")
    ap.add_argument("--arch", default=None, help="linux arch (default: host)")
    ap.add_argument("--workspace", default="/workspace")
    ap.add_argument("--medium", default="initramfs",
                    choices=sorted(_MEDIUM_FILENAME) + ["auto"],
                    help="rootfs medium (auto: pick by image size/packages)")
    ap.add_argument("--package", action="append", default=[], metavar="PKG",
                    help="pip package to layer in (repeatable)")
    ap.add_argument("--deb", action="append", default=[], metavar="NAME",
                    help="pinned system package to layer in (repeatable)")
    ap.add_argument("--force", action="store_true", help="rebuild even if cached")
    args = ap.parse_args(argv)

    r = build(args.ref, arch=args.arch, workspace=args.workspace,
              force=args.force, medium=args.medium, packages=args.package,
              debs=args.deb)
    size_mb = r.rootfs_path.stat().st_size / 1e6
    print(f"ref     {r.ref}")
    print(f"digest  {r.digest}")
    print(f"spec    {r.spec}")
    print(f"rootfs  {r.rootfs_path}  ({size_mb:.1f} MB, {r.medium})")
    print(f"env     {len(r.env)} vars; workdir {r.workdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
