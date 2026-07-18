"""Guest kernel assets: versioned, digest-pinned, fetched on demand.

The VM rungs boot a host-side kernel (vfkit wants an uncompressed
arm64 ``Image``). dud doesn't build kernels; it pins known-good
prebuilt ones and fetches them into ``~/.dud/kernels/<arch>/``. The
current source is the Kata Containers release kernel — a purpose-built
VM kernel with everything dud's ladder needs compiled in (virtio,
vsock, overlayfs, virtiofs, virtio-rng, ext4 — all ``=y``), and the
same kernel Apple's containerization stack points at for
Virtualization.framework guests.

Install layout under ``~/.dud/kernels/<arch>/``:

  ``Image``      — the kernel, at the exact path the vfkit backend's
                   default lookup already probes
  ``meta.json``  — provenance: spec name, kernel version, source URL,
                   digests

Fetching costs a large download (the kernel rides inside Kata's static
release tarball); both the tarball and the extracted kernel are
digest-verified. Extraction shells out to ``zstd`` (``brew install
zstd``) — the one extra host tool, needed only at fetch time.

CLI: ``python -m dud.kernels [--arch ...] [--force]``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from .images import dud_home


class KernelFetchError(Exception):
    """A kernel fetch failed: download, digest, tooling, or extraction."""


@dataclass(frozen=True)
class KernelSpec:
    """A pinned prebuilt kernel and where to get it."""

    name: str            # spec identity, e.g. "kata-3.32.0"
    kernel: str          # kernel version inside, e.g. "6.18.35"
    url: str             # source archive (tar.zst)
    archive_sha256: str  # pin for the archive
    member: str          # path of the kernel inside the archive
    image_sha256: str    # pin for the extracted kernel


KERNELS: dict[str, KernelSpec] = {
    "arm64": KernelSpec(
        name="kata-3.32.0",
        kernel="6.18.35",
        url=(
            "https://github.com/kata-containers/kata-containers/releases/"
            "download/3.32.0/kata-static-3.32.0-arm64.tar.zst"
        ),
        archive_sha256=(
            "8736c054d9223974735394f822000823baef509e1c33405ec798240fa9b6e4b5"
        ),
        member="./opt/kata/share/kata-containers/vmlinux-6.18.35-197",
        image_sha256=(
            "f437320bab94f19105d12b932aa29735f0d54d2588218872254367f312c1027c"
        ),
    ),
}


def kernel_dir(arch: str, home: Path | None = None) -> Path:
    return (home or dud_home()) / "kernels" / arch


def installed(arch: str, home: Path | None = None) -> KernelSpec | None:
    """The spec recorded by a previous install, if the Image is intact."""
    d = kernel_dir(arch, home)
    try:
        meta = json.loads((d / "meta.json").read_text())
    except (OSError, ValueError):
        return None
    if not (d / "Image").is_file():
        return None
    try:
        return KernelSpec(**meta)
    except TypeError:
        return None


def _download(url: str, dest: Path, progress) -> None:
    with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            if progress and total:
                progress(f"download {done // (1 << 20)}/{total // (1 << 20)} MiB")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _extract_member(archive: Path, member: str, dest: Path) -> None:
    """Stream one member out of a .tar.zst without unpacking the rest."""
    zstd = shutil.which("zstd")
    if not zstd:
        raise KernelFetchError("zstd not found (brew install zstd)")
    proc = subprocess.Popen(
        [zstd, "-dc", str(archive)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    assert proc.stdout is not None
    found = False
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tf:
            for info in tf:
                if info.name.lstrip("./") == member.lstrip("./"):
                    src = tf.extractfile(info)
                    if src is None:
                        break
                    with open(dest, "wb") as out:
                        shutil.copyfileobj(src, out)
                    found = True
                    break
    finally:
        proc.stdout.close()
        proc.terminate()
        proc.wait()
    if not found:
        raise KernelFetchError(f"{member} not found in {archive.name}")


def install(
    arch: str,
    home: Path | None = None,
    force: bool = False,
    progress=None,
) -> Path:
    """Ensure the pinned kernel for ``arch`` is installed; return its path.

    Skips work when ``meta.json`` already records the pinned spec.
    Download and extraction are staged in a temp dir and the final
    ``Image``/``meta.json`` land atomically.
    """
    spec = KERNELS.get(arch)
    if spec is None:
        raise KernelFetchError(f"no pinned kernel for arch {arch!r}")
    d = kernel_dir(arch, home)
    have = installed(arch, home)
    if have == spec and not force:
        return d / "Image"

    d.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=d) as tmp:
        archive = Path(tmp) / "archive.tar.zst"
        if progress:
            progress(f"fetching {spec.url}")
        _download(spec.url, archive, progress)
        got = _sha256(archive)
        if got != spec.archive_sha256:
            raise KernelFetchError(
                f"archive digest mismatch: got {got}, want {spec.archive_sha256}"
            )
        if progress:
            progress(f"extracting {spec.member}")
        image = Path(tmp) / "Image"
        _extract_member(archive, spec.member, image)
        got = _sha256(image)
        if got != spec.image_sha256:
            raise KernelFetchError(
                f"kernel digest mismatch: got {got}, want {spec.image_sha256}"
            )
        image.rename(d / "Image")
    (d / "meta.json").write_text(json.dumps(asdict(spec), indent=2))
    return d / "Image"


def main(argv: list[str] | None = None) -> int:
    from .backends.vfkit import _host_arch

    ap = argparse.ArgumentParser(prog="dud.kernels")
    ap.add_argument("--arch", default=None, help="guest arch (default: host)")
    ap.add_argument("--home", default=None, help="dud home (default: ~/.dud)")
    ap.add_argument("--force", action="store_true", help="refetch even if installed")
    ns = ap.parse_args(argv)
    arch = ns.arch or _host_arch()
    home = Path(ns.home) if ns.home else None

    def progress(msg: str) -> None:
        print(f"\r{msg}", end="", file=sys.stderr, flush=True)

    try:
        path = install(arch, home=home, force=ns.force, progress=progress)
    except KernelFetchError as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return 1
    print(file=sys.stderr)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
