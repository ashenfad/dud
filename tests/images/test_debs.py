"""Pinned-deb layering: ar parsing, payload fold, digest pins (no network)."""

from __future__ import annotations

import hashlib
import io
import tarfile

import pytest

from dud.images import debs
from dud.images.cpio import FileSet, is_symlink
from dud.images.debs import DebError, DebSpec, add_deb_tree, fetch_deb


def _ar(members: list[tuple[str, bytes]]) -> bytes:
    """Minimal ar writer (the .deb container format)."""
    out = io.BytesIO()
    out.write(b"!<arch>\n")
    for name, payload in members:
        header = (
            f"{name:<16}" f"{0:<12}" f"{0:<6}" f"{0:<6}" f"{0o100644:<8o}"
            f"{len(payload):<10}"
        ).encode() + b"`\n"
        out.write(header + payload)
        if len(payload) % 2:
            out.write(b"\n")
    return out.getvalue()


def _data_tar_xz(entries: dict[str, bytes | str]) -> bytes:
    """data.tar.xz with files (bytes) and symlinks (str target)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tf:
        seen_dirs = set()
        for path, content in entries.items():
            parent = "/".join(path.split("/")[:-1])
            parts = parent.split("/") if parent else []
            for i in range(1, len(parts) + 1):
                d = "./" + "/".join(parts[:i])
                if d not in seen_dirs:
                    seen_dirs.add(d)
                    info = tarfile.TarInfo(d)
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    tf.addfile(info)
            info = tarfile.TarInfo(f"./{path}")
            if isinstance(content, str):
                info.type = tarfile.SYMTYPE
                info.linkname = content
                tf.addfile(info)
            else:
                info.size = len(content)
                info.mode = 0o755
                tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _fake_deb(entries: dict[str, bytes | str]) -> bytes:
    return _ar([
        ("debian-binary", b"2.0\n"),
        ("control.tar.xz", b"irrelevant"),
        ("data.tar.xz", _data_tar_xz(entries)),
    ])


def test_add_deb_tree_folds_payload(tmp_path):
    deb = tmp_path / "tool.deb"
    deb.write_bytes(_fake_deb({
        "usr/sbin/mkfs.tool": b"\x7fELF-tool",
        "usr/share/doc/tool/copyright": b"GPL",
    }))
    fs = FileSet()
    add_deb_tree(fs, deb)
    assert fs.nodes["usr/sbin/mkfs.tool"].data == b"\x7fELF-tool"
    assert fs.nodes["usr/sbin/mkfs.tool"].mode & 0o111  # executable
    assert fs.nodes["usr/sbin/mkfs.tool"].uid == 0


def test_add_deb_tree_resolves_merged_usr_symlinks(tmp_path):
    """A deb writing sbin/x on a merged-usr tree lands in usr/sbin/x."""
    deb = tmp_path / "tool.deb"
    deb.write_bytes(_fake_deb({"sbin/tool": b"bin"}))
    fs = FileSet()
    fs.add_dir("usr/sbin")
    fs.add_symlink("sbin", "usr/sbin")
    add_deb_tree(fs, deb)
    assert fs.nodes["usr/sbin/tool"].data == b"bin"
    assert "sbin/tool" not in fs.nodes
    assert is_symlink(fs.nodes["sbin"].mode)


def test_bad_ar_magic_raises(tmp_path):
    deb = tmp_path / "bad.deb"
    deb.write_bytes(b"not an archive")
    with pytest.raises(DebError, match="bad magic"):
        add_deb_tree(FileSet(), deb)


def test_missing_data_tar_raises(tmp_path):
    deb = tmp_path / "empty.deb"
    deb.write_bytes(_ar([("debian-binary", b"2.0\n")]))
    with pytest.raises(DebError, match="no data.tar"):
        add_deb_tree(FileSet(), deb)


def test_zstd_data_tar_rejected_loudly(tmp_path):
    deb = tmp_path / "zst.deb"
    deb.write_bytes(_ar([("data.tar.zst", b"\x28\xb5\x2f\xfd")]))
    with pytest.raises(DebError, match="unsupported"):
        add_deb_tree(FileSet(), deb)


def test_fetch_deb_verifies_digest(tmp_path, monkeypatch):
    payload = _fake_deb({"usr/bin/x": b"x"})
    spec = DebSpec(
        name="x", version="1", arch="arm64",
        url="https://example.invalid/x.deb",
        sha256=hashlib.sha256(payload).hexdigest(),
    )

    class FakeResp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        debs.urllib.request, "urlopen",
        lambda url, timeout: FakeResp(payload),
    )
    path = fetch_deb(spec, tmp_path)
    assert path.read_bytes() == payload
    # cached: a second fetch never touches the network
    monkeypatch.setattr(
        debs.urllib.request, "urlopen",
        lambda url, timeout: (_ for _ in ()).throw(AssertionError("net hit")),
    )
    assert fetch_deb(spec, tmp_path) == path


def test_fetch_deb_digest_mismatch_raises(tmp_path, monkeypatch):
    payload = _fake_deb({"usr/bin/x": b"x"})
    spec = DebSpec(
        name="x", version="1", arch="arm64",
        url="https://example.invalid/x.deb", sha256="0" * 64,
    )

    class FakeResp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        debs.urllib.request, "urlopen",
        lambda url, timeout: FakeResp(payload),
    )
    with pytest.raises(DebError, match="digest mismatch"):
        fetch_deb(spec, tmp_path)


def test_unknown_pin_raises():
    with pytest.raises(DebError, match="no pinned deb"):
        debs.deb_spec("nonexistent-tool", "arm64")
