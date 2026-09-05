"""Flatten OCI layers into a FileSet and inject the dud guest runtime.

Applies layers in order with OCI whiteout semantics (``.wh.<name>``
deletes; ``.wh..wh..opq`` clears a directory), forcing every entry to
``uid/gid 0``. Then injects the pure-stdlib ``dud`` package into the
image's ``site-packages`` and writes ``/init`` — a python shebang script
the kernel runs as PID 1 (see ``dud.guest.init``). Device nodes are
skipped: the guest init mounts ``devtmpfs`` on ``/dev``.
"""

from __future__ import annotations

import logging
import posixpath
import sys
import tarfile
from pathlib import Path

from . import registry
from .cpio import (FileSet, Node, S_IFDIR, S_IFLNK, S_IFREG, is_dir,
                   is_reg, is_symlink)

_log = logging.getLogger(__name__)

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


def _lookup(fs: FileSet, layer_nodes: dict, path: str) -> "Node | None":
    """A path's node in the merged view (this layer over the lower fs)."""
    node = layer_nodes.get(path)
    return node if node is not None else fs.nodes.get(path)


def _resolve_parents(fs: FileSet, layer_nodes: dict, path: str) -> str | None:
    """Rewrite ``path`` through any symlinked ancestor dirs.

    Real tar extraction follows symlinks when descending into parent
    directories — on merged-usr Debian a layer writing ``lib/foo`` lands
    in ``usr/lib/foo`` because ``/lib`` is a symlink. Mirror that here so
    the flattened tree matches what a real extraction would produce.
    Returns None for escapes and symlink loops (entry dropped, like
    ``_safe``).
    """
    parts = path.split("/")
    prefix = ""
    for comp in parts[:-1]:
        cand = prefix + comp
        for _ in range(40):  # bounded chase; a loop drops the entry
            node = _lookup(fs, layer_nodes, cand)
            if node is None or not is_symlink(node.mode):
                break
            target = node.data.decode()
            if target.startswith("/"):
                nxt = target
            else:
                nxt = posixpath.join(posixpath.dirname(cand), target)
            resolved = _safe(nxt)
            if resolved is None:
                return None
            cand = resolved
        else:
            return None
        prefix = cand + "/"
    return prefix + parts[-1]


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
                    resolved = _resolve_parents(fs, layer_nodes, path)
                    if resolved is not None:
                        _collect_entry(fs, layer_nodes, tf, m, resolved)

    for d in opaque_dirs:
        prefix = (d + "/") if d else ""
        for key in [k for k in fs.nodes if k != d and k.startswith(prefix)]:
            del fs.nodes[key]
    for target in whiteouts:
        fs.remove_subtree(target)
    fs.nodes.update(layer_nodes)


def _collect_entry(
    fs: FileSet, dst: dict, tf: tarfile.TarFile, m: tarfile.TarInfo, path: str
) -> None:
    perm = m.mode & 0o7777
    if m.isdir():
        existing = _lookup(fs, dst, path)
        if existing is not None and is_symlink(existing.mode):
            # A dir entry over an existing symlink keeps the symlink
            # (tar semantics on merged-usr trees: ./sbin in a payload
            # must not clobber sbin -> usr/sbin); descendants resolve
            # through it via _resolve_parents.
            return
        dst[path] = Node(mode=S_IFDIR | (perm or 0o755))
    elif m.issym():
        dst[path] = Node(mode=S_IFLNK | 0o777, data=m.linkname.encode())
    elif m.islnk():
        # Hardlink: adopt the target's contents (this layer, else lower).
        src = _safe(m.linkname)
        if src is not None:
            src = _resolve_parents(fs, dst, src)
        node = _lookup(fs, dst, src) if src else None
        if node is None:
            # A silently missing file in a booted image is undebuggable;
            # a broken image should fail at build time.
            raise ValueError(
                f"hardlink {path!r} -> {m.linkname!r}: target not found"
            )
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
        if is_dir(n.mode)
        and k.startswith("usr/local/lib/python3.")
        and k.endswith("/site-packages")
    )
    if candidates:
        return candidates[0]
    # Fall back to a versionless path we put on sys.path via /init.
    return "opt/dud"


# Host-only code the guest never imports: excluded from injection so
# edits to it don't bust the rootfs cache (the spec hash covers exactly
# the injected set). dud/__init__ pulls backends.subprocess -> base, so
# those two stay; everything that drives or pools a VMM is host
# machinery.
#
# `vm.py` and `golden.py` were shipped for a while, and the giveaway
# that they never belonged is that they could not have run: both import
# `dud.images`, which is itself host-only and therefore absent from the
# image. So they were unimportable weight whose only effect was to bust
# the rootfs cache whenever host-side VMM code changed.
_HOST_ONLY = {
    ("dud", "images"),
    ("dud", "kernels.py"),
    ("dud", "backends", "vfkit.py"),
    ("dud", "backends", "firecracker.py"),
    ("dud", "backends", "pool.py"),
    ("dud", "backends", "vm.py"),
    ("dud", "backends", "golden.py"),
}


def _dud_package_files() -> dict[str, bytes]:
    """The guest runtime's .py files, keyed by path relative to the package."""
    pkg_root = Path(__file__).resolve().parent.parent  # .../dud
    out: dict[str, bytes] = {}
    for py in sorted(pkg_root.rglob("*.py")):
        rel = py.relative_to(pkg_root.parent)  # dud/....py
        if any(rel.parts[:len(x)] == x for x in _HOST_ONLY):
            continue
        out[str(rel)] = py.read_bytes()
    return out


def inject_dud(fs: FileSet, extra_pythonpath: str | None = None) -> str:
    """Install the dud package into site-packages. Returns its parent dir."""
    site = _site_packages(fs)
    for rel, data in _dud_package_files().items():
        fs.add_file(f"{site}/{rel}", data, 0o644)
    return site


def _image_env(env: list[str]) -> dict[str, str]:
    """The image's ENV as a mapping, dropping anything malformed.

    Baked into /init so the guest boots with the environment its image
    declares. Without it the kernel hands init almost nothing and there
    is no PATH at all — which bash hides behind its own fallback while
    every other PATH consumer (subprocess, timeout, xargs, and CPython
    resolving its own argv[0]) fails to find /usr/local/bin, where the
    image keeps python, pip and every console script.
    """
    out: dict[str, str] = {}
    for entry in env or ():
        key, sep, value = entry.partition("=")
        if sep and key:
            out[key] = value
    return out


def _init_script(site: str, workspace: str,
                 env: dict[str, str] | None = None) -> bytes:
    lines = [
        "#!/usr/local/bin/python3",
        "import os, sys",
        # Overrides, not defaults. What the kernel hands PID 1 is two
        # hardcoded constants — HOME=/ and TERM=linux — not choices made
        # for this machine, so deferring to them would silently discard
        # an image that declares either (ENV HOME=/app is ordinary for
        # app images). The image's ENV is the deliberate statement, and
        # treating it as authoritative is also what a container runtime
        # does, which is the behavior being matched.
        #
        # dud's own variables are applied AFTER this, in guest.init and
        # the supervisor, so they still win. And note what is NOT
        # applied — the image's WORKDIR. Execs start at the workspace
        # root by contract, which is a dud decision the image gets no
        # vote in.
        f"os.environ.update({dict(env or {})!r})",
        f"sys.path.insert(0, {('/' + site)!r})",
        "from dud.guest.init import main",
        f"main(default_root={workspace!r})",
        "",
    ]
    return "\n".join(lines).encode()


def _guest_py_version(site: str) -> str | None:
    """The guest's Python minor version, read off its site-packages path."""
    for part in site.split("/"):
        if part.startswith("python3."):
            return part[len("python"):]
    return None


def bytecode_status(site: str) -> str:
    """Whether this image ships precompiled bytecode, and if not, why.

    One fact decides it for both bakes — the stdlib and dud runtime
    here, and the layered wheels in ``builder._bytecompile`` — because
    bytecode is minor-version scoped: neither can emit a ``.pyc`` the
    guest will load unless the host interpreter matches the image's.

    Worth naming rather than leaving implicit in two ``if`` statements,
    because the failure is silent and expensive. The default image is
    ``python:3.12-slim`` while the tested host is increasingly 3.13 or
    3.14, so a developer on a current Python gets *no* baked bytecode
    at all and pays a recompile of the stdlib on every exec — with
    nothing raised, nothing logged at the seam that matters, and no way
    for CI to notice, since CI pins the matching version precisely so
    that it does bake.
    """
    py = _guest_py_version(site)
    host = f"{sys.version_info.major}.{sys.version_info.minor}"
    if py is None:
        return "skipped: no python version in the image layout"
    if py != host:
        return f"skipped: host python {host} != guest {py}"
    return "baked"


def bake_pyc(fs: FileSet, site: str) -> int:
    """Compile every module in the rootfs to bytecode, ahead of boot.

    The base image ships none: docker-library's python:*-slim deletes
    every .pyc at build time, and dud's own runtime is injected as
    source. So the guest was compiling ~1100 stdlib modules from source
    on the way up — and on an erofs root, which is mounted read-only,
    it could never write the result, so it paid that on every exec
    forever rather than once.

    Bytecode is minor-version-scoped, so this only bakes when the host
    interpreter matches the guest's; a mismatch skips silently and
    costs speed, not correctness. That is the same rule the layered
    wheels have always used — this extends it to the two much larger
    sets that were never covered.

    UNCHECKED_HASH, like the wheels: sources in a rootfs never change
    underneath their bytecode, so validation would be a stat per
    import for an answer that cannot vary — and it dodges the
    deterministic zero mtimes the image is built with.
    """
    import importlib.util
    from importlib._bootstrap_external import _code_to_hash_pyc

    status = bytecode_status(site)
    if status != "baked":
        _log.info("shipping without bytecode (%s): guest imports recompile "
                  "from source on every exec, same behavior", status)
        return 0

    tag = sys.implementation.cache_tag
    baked = 0
    for path in [k for k in list(fs.nodes) if k.endswith(".py")]:
        node = fs.nodes[path]
        # Regular files only. `Node.data` holds the symlink TARGET for a
        # symlink, so a `foo.py -> bar.py` would compile the string
        # "bar.py" into perfectly well-formed bytecode for the
        # expression `bar.py` — and since these are written
        # UNCHECKED_HASH, importing foo would run it without ever
        # consulting the real source, raising NameError instead. A
        # directory named `*.py` lands here too.
        if not is_reg(node.mode):
            continue
        src = node.data
        try:
            # optimize=0 pins what the plain `module.<tag>.pyc` name
            # promises. `dont_inherit` covers __future__ flags but NOT
            # the optimization level, which defaults to -1: inherited
            # from the builder's own interpreter. Under `python -O` or
            # PYTHONOPTIMIZE we would bake assert-stripped, __debug__
            # False bytecode under the name a normally-started guest
            # loads, silently changing its runtime semantics.
            code = compile(src, "/" + path, "exec",
                           dont_inherit=True, optimize=0)
        except (SyntaxError, ValueError):
            # Test fixtures and py2 leftovers live in the stdlib tree;
            # one unparseable file must not fail an image build.
            continue
        pyc = _code_to_hash_pyc(code, importlib.util.source_hash(src), False)
        parent, _, name = path.rpartition("/")
        fs.add_file(f"{parent}/__pycache__/{name[:-3]}.{tag}.pyc", pyc, 0o644)
        baked += 1
    _log.info("baked %d .pyc into the rootfs", baked)
    return baked


_INTERPRETER = "usr/local/bin/python3"

#: ``dud-emit`` on the guest's PATH. A shim, not a copy: the logic
#: lives in ``dud.guest.emit``, which is injected into site-packages
#: like the rest of the guest runtime — so editing it moves
#: ``_dud_code_hash`` and busts the rootfs cache, which a script
#: generated from here does not (``dud/images`` is _HOST_ONLY, the same
#: trap ``/init`` carries; hence the PIPELINE_VERSION bump that shipped
#: with this).
_EMIT_SHIM = b"""#!/usr/local/bin/python3
import sys
from dud.guest.emit import main
sys.exit(main())
"""

#: ``dud-hostcall`` on the guest's PATH. Same shim discipline as emit:
#: the logic lives in the injected ``dud.guest.hostcall``, and the
#: script generated here does not move the code hash (see the v6
#: pipeline note in ``builder.py``).
_HOSTCALL_SHIM = b"""#!/usr/local/bin/python3
import sys
from dud.guest.hostcall import main
sys.exit(main())
"""


def build_fileset(
    image: registry.PulledImage, workspace: str = "/workspace"
) -> FileSet:
    """Full rootfs: flattened image + dud runtime + /init entrypoint."""
    fs = flatten_layers(image.layer_paths)
    # /init's shebang hardcodes the docker-official-python layout; an
    # image without it would boot to a kernel panic, so fail at build.
    if _INTERPRETER not in fs.nodes:
        raise ValueError(
            f"image has no /{_INTERPRETER}; dud guests currently require "
            f"a python:*-slim-style layout"
        )
    site = inject_dud(fs)
    fs.add_dir(workspace, 0o755)
    # After injection, so dud's own modules are covered too.
    bake_pyc(fs, site)
    fs.add_file("usr/local/bin/dud-emit", _EMIT_SHIM, 0o755)
    fs.add_file("usr/local/bin/dud-hostcall", _HOSTCALL_SHIM, 0o755)
    fs.add_file(
        "init", _init_script(site, workspace, _image_env(image.env)), 0o755
    )
    return fs
