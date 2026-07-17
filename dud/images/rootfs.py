"""Flatten OCI layers into a FileSet and inject the dud guest runtime.

Applies layers in order with OCI whiteout semantics (``.wh.<name>``
deletes; ``.wh..wh..opq`` clears a directory), forcing every entry to
``uid/gid 0``. Then injects the pure-stdlib ``dud`` package into the
image's ``site-packages`` and writes ``/init`` — a python shebang script
the kernel runs as PID 1 (see ``dud.guest.init``). Device nodes are
skipped: the guest init mounts ``devtmpfs`` on ``/dev``.
"""

from __future__ import annotations

import posixpath
import tarfile
from pathlib import Path

from . import registry
from .cpio import FileSet, Node, S_IFDIR, S_IFLNK, S_IFREG

_WH_PREFIX = ".wh."
_WH_OPAQUE = ".wh..wh..opq"


def _safe(name: str) -> str | None:
    """Normalize a tar member path; reject traversal/absolute escapes."""
    p = posixpath.normpath(name).lstrip("/")
    if p in ("", ".") or p.startswith("../") or "/../" in p or p == "..":
        return None
    return p


def flatten_layers(layer_paths: list[Path]) -> FileSet:
    """Merge gzipped layer tars into a single root-owned FileSet."""
    fs = FileSet()
    for layer in layer_paths:
        _apply_layer(fs, layer)
    return fs


def _apply_layer(fs: FileSet, layer_path: Path) -> None:
    """Collect one layer in a single streaming pass, then apply it.

    Whiteouts (regular + opaque) act against the *accumulated* lower
    result, so they are gathered separately and applied before this
    layer's own entries are merged on top — matching OCI semantics where
    an opaque marker hides lower layers but not its own siblings.
    """
    layer_nodes: dict[str, "Node"] = {}
    opaque_dirs: list[str] = []
    whiteouts: list[str] = []

    with registry.open_layer(layer_path) as stream:
        with tarfile.open(fileobj=stream, mode="r|*") as tf:
            for m in tf:
                path = _safe(m.name)
                if path is None:
                    continue
                base = posixpath.basename(path)
                parent = posixpath.dirname(path)

                if base == _WH_OPAQUE:
                    opaque_dirs.append(parent)
                elif base.startswith(_WH_PREFIX):
                    whiteouts.append(
                        posixpath.join(parent, base[len(_WH_PREFIX):])
                        if parent else base[len(_WH_PREFIX):]
                    )
                else:
                    _collect_entry(layer_nodes, tf, m, path)

    for d in opaque_dirs:
        prefix = (d + "/") if d else ""
        for key in [k for k in fs.nodes if k != d and k.startswith(prefix)]:
            del fs.nodes[key]
    for target in whiteouts:
        fs.remove_subtree(target)
    fs.nodes.update(layer_nodes)


def _collect_entry(
    dst: dict, tf: tarfile.TarFile, m: tarfile.TarInfo, path: str
) -> None:
    perm = m.mode & 0o7777
    if m.isdir():
        dst[path] = Node(mode=S_IFDIR | (perm or 0o755))
    elif m.issym():
        dst[path] = Node(mode=S_IFLNK | 0o777, data=m.linkname.encode())
    elif m.islnk():
        # Hardlink: adopt the target's contents (this layer, else lower).
        src = _safe(m.linkname)
        node = dst.get(src) if src else None
        if node is not None:
            dst[path] = Node(mode=node.mode, data=node.data)
    elif m.isreg():
        f = tf.extractfile(m)
        data = f.read() if f is not None else b""
        dst[path] = Node(mode=S_IFREG | (perm or 0o644), data=data)
    # char/block/fifo: skipped by design.


def _site_packages(fs: FileSet) -> str:
    """Find the image's site-packages dir (python:slim ships exactly one)."""
    candidates = sorted(
        k for k, n in fs.nodes.items()
        if (n.mode & S_IFDIR)
        and k.startswith("usr/local/lib/python3.")
        and k.endswith("/site-packages")
    )
    if candidates:
        return candidates[0]
    # Fall back to a versionless path we put on sys.path via /init.
    return "opt/dud"


def _dud_package_files() -> dict[str, bytes]:
    """The guest runtime's .py files, keyed by path relative to the package.

    Excludes ``dud.images`` — the host-side build pipeline never runs in
    the guest, so injecting it would only bloat the rootfs and couple its
    cache key to build-tool edits.
    """
    pkg_root = Path(__file__).resolve().parent.parent  # .../dud
    out: dict[str, bytes] = {}
    for py in sorted(pkg_root.rglob("*.py")):
        rel = py.relative_to(pkg_root.parent)  # dud/....py
        if rel.parts[:2] == ("dud", "images"):
            continue
        out[str(rel)] = py.read_bytes()
    return out


def inject_dud(fs: FileSet, extra_pythonpath: str | None = None) -> str:
    """Install the dud package into site-packages. Returns its parent dir."""
    site = _site_packages(fs)
    for rel, data in _dud_package_files().items():
        fs.add_file(f"{site}/{rel}", data, 0o644)
    return site


def _init_script(site: str, workspace: str) -> bytes:
    lines = [
        "#!/usr/local/bin/python3",
        "import sys",
        f"sys.path.insert(0, {('/' + site)!r})",
        "from dud.guest.init import main",
        f"main(default_root={workspace!r})",
        "",
    ]
    return "\n".join(lines).encode()


def build_fileset(
    image: registry.PulledImage, workspace: str = "/workspace"
) -> FileSet:
    """Full rootfs: flattened image + dud runtime + /init entrypoint."""
    fs = flatten_layers(image.layer_paths)
    site = inject_dud(fs)
    fs.add_dir(workspace, 0o755)
    fs.add_file("init", _init_script(site, workspace), 0o755)
    return fs
