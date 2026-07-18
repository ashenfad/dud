"""Kernel fetch-and-cache: digest pins, idempotence, extraction (no network)."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import tarfile

import pytest

from dud import kernels
from dud.kernels import KernelFetchError, KernelSpec, install, installed

pytestmark = pytest.mark.skipif(
    shutil.which("zstd") is None, reason="zstd binary not available"
)

_KERNEL_BYTES = b"\x00fake-arm64-Image\x00" * 64


def _make_archive(tmp_path, member="./opt/share/vmlinux-test"):
    """A tiny tar.zst holding one fake kernel plus a decoy."""
    tar_path = tmp_path / "a.tar"
    with tarfile.open(tar_path, "w") as tf:
        for name, data in [("./opt/share/decoy", b"nope"), (member, _KERNEL_BYTES)]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    zst_path = tmp_path / "a.tar.zst"
    subprocess.run(["zstd", "-q", str(tar_path), "-o", str(zst_path)], check=True)
    return zst_path


def _spec(zst_path, member="./opt/share/vmlinux-test", **overrides):
    fields = dict(
        name="test-1",
        kernel="0.0",
        url="https://example.invalid/a.tar.zst",
        archive_sha256=hashlib.sha256(zst_path.read_bytes()).hexdigest(),
        member=member,
        image_sha256=hashlib.sha256(_KERNEL_BYTES).hexdigest(),
    )
    fields.update(overrides)
    return KernelSpec(**fields)


@pytest.fixture
def fetchable(tmp_path, monkeypatch):
    """Pin a fake spec and route _download at the local archive."""
    zst = _make_archive(tmp_path)
    spec = _spec(zst)
    monkeypatch.setitem(kernels.KERNELS, "testarch", spec)
    calls = []

    def fake_download(url, dest, progress):
        calls.append(url)
        shutil.copyfile(zst, dest)

    monkeypatch.setattr(kernels, "_download", fake_download)
    return spec, calls, tmp_path / "home"


def test_install_fetches_verifies_and_records(fetchable):
    spec, calls, home = fetchable
    path = install("testarch", home=home)
    assert path.read_bytes() == _KERNEL_BYTES
    meta = json.loads((path.parent / "meta.json").read_text())
    assert meta["name"] == "test-1" and calls == [spec.url]
    assert installed("testarch", home) == spec


def test_install_is_idempotent(fetchable):
    spec, calls, home = fetchable
    install("testarch", home=home)
    install("testarch", home=home)
    assert calls == [spec.url]  # second call touched nothing


def test_force_refetches(fetchable):
    spec, calls, home = fetchable
    install("testarch", home=home)
    install("testarch", home=home, force=True)
    assert calls == [spec.url, spec.url]


def test_archive_digest_mismatch_raises(fetchable, monkeypatch):
    spec, _, home = fetchable
    monkeypatch.setitem(
        kernels.KERNELS, "testarch",
        _spec_replace(spec, archive_sha256="0" * 64),
    )
    with pytest.raises(KernelFetchError, match="archive digest"):
        install("testarch", home=home)
    assert installed("testarch", home) is None


def test_kernel_digest_mismatch_raises(fetchable, monkeypatch):
    spec, _, home = fetchable
    monkeypatch.setitem(
        kernels.KERNELS, "testarch",
        _spec_replace(spec, image_sha256="0" * 64),
    )
    with pytest.raises(KernelFetchError, match="kernel digest"):
        install("testarch", home=home)


def test_missing_member_raises(fetchable, monkeypatch):
    spec, _, home = fetchable
    monkeypatch.setitem(
        kernels.KERNELS, "testarch",
        _spec_replace(spec, member="./not/there"),
    )
    with pytest.raises(KernelFetchError, match="not found"):
        install("testarch", home=home)


def test_unpinned_arch_raises(tmp_path):
    with pytest.raises(KernelFetchError, match="no pinned kernel"):
        install("mips", home=tmp_path)


def test_direct_download_spec_skips_extraction(tmp_path, monkeypatch):
    """member=None: the URL is the Image itself — no archive, no zstd."""
    spec = KernelSpec(
        name="direct-1",
        kernel="0.0",
        url="https://example.invalid/Image",
        image_sha256=hashlib.sha256(_KERNEL_BYTES).hexdigest(),
    )
    monkeypatch.setitem(kernels.KERNELS, "testarch", spec)
    monkeypatch.setattr(
        kernels, "_download",
        lambda url, dest, progress: dest.write_bytes(_KERNEL_BYTES),
    )
    monkeypatch.setattr(
        kernels, "_extract_member",
        lambda *a: pytest.fail("direct spec must not extract"),
    )
    home = tmp_path / "home"
    path = install("testarch", home=home)
    assert path.read_bytes() == _KERNEL_BYTES
    assert installed("testarch", home) == spec


def test_direct_download_digest_mismatch_raises(tmp_path, monkeypatch):
    spec = KernelSpec(
        name="direct-1", kernel="0.0",
        url="https://example.invalid/Image", image_sha256="0" * 64,
    )
    monkeypatch.setitem(kernels.KERNELS, "testarch", spec)
    monkeypatch.setattr(
        kernels, "_download",
        lambda url, dest, progress: dest.write_bytes(_KERNEL_BYTES),
    )
    with pytest.raises(KernelFetchError, match="kernel digest"):
        install("testarch", home=tmp_path / "home")


def test_gh_download_declines_non_release_urls(tmp_path):
    assert not kernels._gh_download("https://example.com/f", tmp_path / "f")
    assert not kernels._gh_download(
        "https://github.com/o/r/archive/main.tar.gz", tmp_path / "f"
    )


def test_download_falls_back_to_gh(tmp_path, monkeypatch):
    """Anonymous 404 on a private release asset -> gh CLI path."""
    url = "https://github.com/o/r/releases/download/tag/Image-x"

    def anon_fails(*a, **k):
        raise __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
            url, 404, "Not Found", None, None
        )

    monkeypatch.setattr(kernels.urllib.request, "urlopen", anon_fails)

    def fake_gh(u, dest):
        assert u == url
        dest.write_bytes(_KERNEL_BYTES)
        return True

    monkeypatch.setattr(kernels, "_gh_download", fake_gh)
    dest = tmp_path / "Image"
    kernels._download(url, dest, None)
    assert dest.read_bytes() == _KERNEL_BYTES


def _spec_replace(spec: KernelSpec, **overrides) -> KernelSpec:
    from dataclasses import asdict

    return KernelSpec(**{**asdict(spec), **overrides})
