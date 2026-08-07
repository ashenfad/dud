"""Reference parsing, platform selection, resolution cache (no network)."""

from __future__ import annotations

import hashlib
import json
import threading

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


class _FakeResponse:
    def __init__(self, raw: bytes):
        self._raw = raw

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_manifest_by_digest_verifies_bytes(tmp_path, monkeypatch):
    reg = Registry(tmp_path)
    monkeypatch.setattr(
        Registry, "_get", lambda self, r, p, a: _FakeResponse(b"{}")
    )
    ref = ImageRef.parse("python:3.12-slim")
    with pytest.raises(RegistryError, match="manifest digest mismatch"):
        reg._manifest(ref, "sha256:" + "0" * 64)


def test_expired_token_retried_once_with_fresh_auth(tmp_path, monkeypatch):
    import io
    import urllib.error

    reg = Registry(tmp_path)
    reg._tokens["library/python"] = "stale"
    calls = []

    def fake_urlopen(req, timeout):
        calls.append(req.get_header("Authorization"))
        if len(calls) == 1:
            raise urllib.error.HTTPError(
                req.full_url, 401, "Unauthorized", None, None
            )
        return io.BytesIO(b"ok")

    monkeypatch.setattr(
        "dud.images.registry.urllib.request.urlopen", fake_urlopen
    )
    monkeypatch.setattr(Registry, "_token", lambda self, r: (
        "stale" if not calls else "fresh"
    ))
    ref = ImageRef.parse("python:3.12-slim")
    resp = reg._get(ref, "blobs/sha256:abc", "*/*")
    assert resp.read() == b"ok"
    assert calls == ["Bearer stale", "Bearer fresh"]


def test_concurrent_writers_cannot_publish_a_torn_blob(tmp_path, monkeypatch):
    """Two pulls of one digest must not corrupt the cached blob.

    Routine, not exotic: the pool's background refill boots a session
    on the same key a foreground acquire is already booting, so both
    reach for the same layer with a cold cache. The danger is that the
    digest check verifies the bytes a writer SENT — if writers shared a
    staging path, the second one's truncating open would punch a hole
    in the first one's file, that check would still pass, and the torn
    blob would be published under a name nothing ever re-verifies.
    """
    reg = Registry(tmp_path)
    content = b"layer-content:" + bytes(range(256)) * 64
    hexd = hashlib.sha256(content).hexdigest()
    digest = f"sha256:{hexd}"
    ref = ImageRef.parse("python:3.12-slim")

    slow_has_written = threading.Event()  # slow's staging file exists
    fast_finished = threading.Event()     # fast has published and gone

    class _Body:
        def __init__(self, chunks, pause_before_last=False):
            self._chunks = list(chunks)
            self._pause = pause_before_last

        def read(self, _n=None):
            if not self._chunks:
                return b""
            if self._pause and len(self._chunks) == 1:
                # Our first chunk is on disk and our file is still open:
                # the exact window where a second writer sharing the
                # path would truncate underneath us.
                slow_has_written.set()
                fast_finished.wait(timeout=10)
            return self._chunks.pop(0)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_get(self, r, path, accept):
        if threading.current_thread().name == "slow":
            return _Body([content[:100], content[100:]], pause_before_last=True)
        slow_has_written.wait(timeout=10)  # open our file mid-slow-write
        return _Body([content])

    monkeypatch.setattr(Registry, "_get", fake_get)

    errors: list[Exception] = []

    def pull_blob():
        try:
            reg._blob(ref, digest)
        except Exception as e:  # noqa: BLE001 — reported below
            errors.append(e)
        finally:
            if threading.current_thread().name == "fast":
                fast_finished.set()

    threads = [
        threading.Thread(target=pull_blob, name=name) for name in ("slow", "fast")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert not errors, errors
    assert (reg.blobs / hexd).read_bytes() == content
    assert not list(reg.blobs.glob("*.part*")), "staging residue left behind"


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
