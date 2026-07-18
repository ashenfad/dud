"""Opt-in live pull against Docker Hub — catches registry-API drift.

Skipped unless ``DUD_NETWORK_TESTS=1``; it downloads a real base image
(~40 MB) which the fast unit tests deliberately mock. Run with:

    DUD_NETWORK_TESTS=1 uv run pytest tests/images/test_integration.py
"""

from __future__ import annotations

import gzip
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("DUD_NETWORK_TESTS") != "1",
    reason="set DUD_NETWORK_TESTS=1 to run live registry pulls",
)


def test_pull_and_build_python_slim(tmp_path):
    from dud.images import builder

    r = builder.build("python:3.12-slim", arch="arm64", home=tmp_path)

    assert r.rootfs_path.exists()
    assert r.digest.startswith("sha256:")
    assert any("PATH=" in e for e in r.env)

    raw = gzip.decompress(r.rootfs_path.read_bytes())
    assert raw[:6] == b"070701"
    # A real python:slim carries its interpreter and our injected runtime.
    assert b"usr/local/bin/python3" in raw
    assert b"dud/guest/supervisor.py" in raw
    assert b"from dud.guest.init import main" in raw
