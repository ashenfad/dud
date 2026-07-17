"""Reference parsing and platform selection (no network)."""

from __future__ import annotations

import pytest

from dud.images.registry import ImageRef, Registry, RegistryError


def test_bare_name_resolves_to_library_latest():
    r = ImageRef.parse("python")
    assert (r.registry, r.repository, r.reference) == (
        "registry-1.docker.io", "library/python", "latest",
    )


def test_name_with_tag():
    r = ImageRef.parse("python:3.12-slim")
    assert r.repository == "library/python" and r.reference == "3.12-slim"


def test_user_repo():
    r = ImageRef.parse("astral/uv:latest")
    assert r.repository == "astral/uv" and r.registry == "registry-1.docker.io"


def test_explicit_registry():
    r = ImageRef.parse("ghcr.io/owner/name:v1")
    assert r.registry == "ghcr.io"
    assert r.repository == "owner/name" and r.reference == "v1"


def test_digest_reference():
    r = ImageRef.parse("python@sha256:" + "a" * 64)
    assert r.reference == "sha256:" + "a" * 64
    assert r.repository == "library/python"


def test_select_platform_picks_matching_arch(tmp_path):
    reg = Registry(tmp_path)
    index = {"manifests": [
        {"platform": {"os": "linux", "architecture": "amd64"}, "digest": "sha256:amd"},
        {"platform": {"os": "linux", "architecture": "arm64"}, "digest": "sha256:arm"},
    ]}
    assert reg._select_platform(index, "arm64") == "sha256:arm"


def test_select_platform_missing_arch_raises(tmp_path):
    reg = Registry(tmp_path)
    index = {"manifests": [
        {"platform": {"os": "linux", "architecture": "amd64"}, "digest": "sha256:amd"},
    ]}
    with pytest.raises(RegistryError):
        reg._select_platform(index, "arm64")
