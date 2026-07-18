"""Wheel-layering helpers (no network / no uv invocation)."""

from __future__ import annotations

import pytest

from dud.images import wheels
from dud.images.cpio import FileSet


def test_python_version_from_site():
    assert wheels.python_version_from_site(
        "usr/local/lib/python3.12/site-packages") == "3.12"
    assert wheels.python_version_from_site("opt/dud") == "3.12"  # fallback


def test_add_target_tree_routes_scripts_and_packages(tmp_path):
    # Mimic a `uv --target` layout: a package, a bundled .so, a bin script.
    (tmp_path / "numpy").mkdir()
    (tmp_path / "numpy" / "__init__.py").write_bytes(b"x")
    (tmp_path / "numpy.libs").mkdir()
    (tmp_path / "numpy.libs" / "libopenblas.so").write_bytes(b"\x7fELF")
    (tmp_path / "bin").mkdir()
    script = tmp_path / "bin" / "f2py"
    script.write_bytes(b"#!/usr/bin/env python\n")
    script.chmod(0o755)

    fs = FileSet()
    site = "usr/local/lib/python3.12/site-packages"
    wheels.add_target_tree(fs, tmp_path, site)

    assert f"{site}/numpy/__init__.py" in fs.nodes
    assert f"{site}/numpy.libs/libopenblas.so" in fs.nodes
    # scripts route to /usr/local/bin, and stay executable
    node = fs.nodes["usr/local/bin/f2py"]
    assert node.mode & 0o111


def test_resolve_wheels_requires_uv(tmp_path, monkeypatch):
    monkeypatch.setattr(wheels.shutil, "which", lambda _: None)
    with pytest.raises(wheels.WheelError):
        wheels.resolve_wheels(["numpy"], tmp_path, "arm64", "3.12")


def test_resolve_wheels_unknown_arch(tmp_path, monkeypatch):
    monkeypatch.setattr(wheels.shutil, "which", lambda _: "/usr/bin/uv")
    with pytest.raises(wheels.WheelError):
        wheels.resolve_wheels(["numpy"], tmp_path, "sparc", "3.12")
