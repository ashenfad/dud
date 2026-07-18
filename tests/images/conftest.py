"""Helpers for building synthetic OCI layers without a network."""

from __future__ import annotations

import gzip
import io
import tarfile
from pathlib import Path

import pytest


@pytest.fixture
def make_layer(tmp_path):
    """Factory: write a gzipped layer tar under tmp_path, return its path.

    ``whiteouts`` are literal member names (e.g. ``a/.wh.b`` or
    ``a/.wh..wh..opq``) added as empty regular files, matching the OCI
    on-disk convention.
    """
    def _make(
        name: str,
        files: dict[str, bytes | str] | None = None,
        dirs: list[str] | None = None,
        symlinks: dict[str, str] | None = None,
        hardlinks: dict[str, str] | None = None,
        whiteouts: list[str] | None = None,
    ) -> Path:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for d in dirs or []:
                info = tarfile.TarInfo(d)
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tf.addfile(info)
            for path, content in (files or {}).items():
                data = content.encode() if isinstance(content, str) else content
                info = tarfile.TarInfo(path)
                info.size = len(data)
                info.mode = 0o644
                tf.addfile(info, io.BytesIO(data))
            for link, target in (symlinks or {}).items():
                info = tarfile.TarInfo(link)
                info.type = tarfile.SYMTYPE
                info.linkname = target
                tf.addfile(info)
            for link, target in (hardlinks or {}).items():
                info = tarfile.TarInfo(link)
                info.type = tarfile.LNKTYPE
                info.linkname = target
                tf.addfile(info)
            for wh in whiteouts or []:
                info = tarfile.TarInfo(wh)
                info.size = 0
                tf.addfile(info, io.BytesIO(b""))
        path = tmp_path / name
        path.write_bytes(gzip.compress(buf.getvalue()))
        return path

    return _make
