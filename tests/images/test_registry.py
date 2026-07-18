"""Reference parsing, platform selection, resolution cache (no network)."""

from __future__ import annotations

import hashlib
import json

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


def _seed_blob(reg: Registry, data: bytes) -> str:
    """Drop data into the blob cache; return its digest reference."""
    hexd = hashlib.sha256(data).hexdigest()
    (reg.blobs / hexd).write_bytes(data)
    return f"sha256:{hexd}"


def _fake_manifest(reg: Registry) -> dict:
    config = json.dumps({"config": {"Env": ["A=1"], "WorkingDir": "/w"}}).encode()
    layer = b"layer-bytes"
    return {
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"digest": _seed_blob(reg, config)},
        "layers": [{"digest": _seed_blob(reg, layer)}],
    }


def test_pull_caches_resolution_and_survives_registry_loss(tmp_path, monkeypatch):
    reg = Registry(tmp_path)
    manifest = _fake_manifest(reg)

    monkeypatch.setattr(
        Registry, "_resolve", lambda self, r, arch: (manifest, "sha256:mfst")
    )
    img = reg.pull("python:3.12-slim", arch="arm64")
    assert img.digest == "sha256:mfst" and img.env == ["A=1"]

    # Registry goes away (outage / 429): the cached resolution serves.
    def down(self, r, arch):
        raise RegistryError("GET manifests/x -> 429 Too Many Requests")

    monkeypatch.setattr(Registry, "_resolve", down)
    again = reg.pull("python:3.12-slim", arch="arm64")
    assert again.digest == "sha256:mfst"
    assert again.layer_paths == img.layer_paths


def test_pull_with_no_cache_propagates_registry_error(tmp_path, monkeypatch):
    reg = Registry(tmp_path)

    def down(self, r, arch):
        raise RegistryError("GET manifests/x -> 429 Too Many Requests")

    monkeypatch.setattr(Registry, "_resolve", down)
    with pytest.raises(RegistryError):
        reg.pull("python:3.12-slim", arch="arm64")


def test_pull_cache_is_per_arch(tmp_path, monkeypatch):
    reg = Registry(tmp_path)
    manifest = _fake_manifest(reg)

    monkeypatch.setattr(
        Registry, "_resolve", lambda self, r, arch: (manifest, "sha256:mfst")
    )
    reg.pull("python:3.12-slim", arch="arm64")

    def down(self, r, arch):
        raise RegistryError("down")

    monkeypatch.setattr(Registry, "_resolve", down)
    with pytest.raises(RegistryError):
        reg.pull("python:3.12-slim", arch="amd64")  # no cache for this arch
