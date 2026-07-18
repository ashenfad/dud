"""HostSession boundary hygiene (no guest process needed)."""

from __future__ import annotations

import pytest

from dud.backends.base import _safe_diff_path
from dud.proto import ProtocolError


def test_safe_diff_path_passes_normal_paths():
    assert _safe_diff_path("a/b.txt") == "a/b.txt"
    assert _safe_diff_path("./a/./b") == "a/b"


def test_safe_diff_path_normalizes_absolute_to_relative():
    assert _safe_diff_path("/etc/passwd") == "etc/passwd"


def test_safe_diff_path_rejects_traversal():
    for evil in ("../x", "a/../../x", "..", "a/b/../../../c"):
        with pytest.raises(ProtocolError):
            _safe_diff_path(evil)
