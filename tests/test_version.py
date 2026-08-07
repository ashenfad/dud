"""``dud.__version__`` tracks the packaging metadata, not a literal."""

from __future__ import annotations

import tomllib
from pathlib import Path

import dud

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_version_matches_pyproject():
    """The regression this guards: `__version__` was a hardcoded string
    and sat at 0.0.1 through two releases, because a version bump only
    ever touched pyproject. Deriving it removes the second place to
    forget — and this pins that it stays derived.

    A failure here almost always means the editable install is stale
    rather than the code being wrong: re-run `uv sync` (or `pip install
    -e .`) so the metadata catches up with pyproject.
    """
    declared = tomllib.loads(_PYPROJECT.read_text())["project"]["version"]
    assert dud.__version__ == declared


def test_version_is_not_a_module_literal():
    """Belt and braces: nothing may reintroduce the literal. A module
    attribute would shadow the PEP 562 __getattr__ that reads metadata.
    """
    assert "__version__" not in vars(dud)
    assert dud.__version__ == dud._installed_version()
