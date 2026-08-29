"""The outputs hook: dud offers rich `ui` values, the image shapes them.

dud used to carry this itself, in a guest module that knew plotly
figures serialize to `ui/<name>.plotly.json` and that DataFrames get
`head(200)`. Those are the consuming layer's conventions, so they now
live in a package the image supplies. What is tested here is dud's half
of the contract: find the hook, offer it the values, respect what it
claims, survive it misbehaving, and never resolve it from files the
agent wrote.
"""

from __future__ import annotations

import sys
import types

import pytest

from dud.guest import runner


@pytest.fixture(autouse=True)
def _unresolved():
    """The hook and renderer cache per runner process (one per exec);
    tests need each case resolved fresh."""
    runner._OUTPUTS_HOOK = None
    runner._RENDERER = None
    yield
    runner._OUTPUTS_HOOK = None
    runner._RENDERER = None


def _install(monkeypatch, flatten, name: str = "dud_outputs"):
    mod = types.ModuleType(name)
    if flatten is not None:
        mod.flatten = flatten
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


def test_no_hook_means_nothing_is_flattened(monkeypatch):
    """The zero-knowledge default. A consumer with no convention gets
    unrepresentable values reported in outputs_skipped, not invented
    files in its workspace."""
    monkeypatch.delitem(sys.modules, "dud_outputs", raising=False)
    g = {"ui": {"chart": object()}}
    runner._flatten_ui(g)
    assert set(g["ui"]) == {"chart"}  # untouched, still unrepresentable


def test_handled_names_are_dropped_and_the_rest_crosses(monkeypatch):
    """The partition: what the hook claims it wrote is removed from
    `ui`, and the representable remainder still harvests to the host."""
    seen = {}

    def flatten(ui, workspace):
        seen["ui"], seen["workspace"] = ui, workspace
        return {"chart"}

    _install(monkeypatch, flatten)
    monkeypatch.setenv("DUD_WORKSPACE", "/workspace")
    g = {"ui": {"chart": object(), "cards": [{"label": "x", "value": 1}]}}
    runner._flatten_ui(g)
    assert set(g["ui"]) == {"cards"}
    assert set(seen["ui"]) == {"chart", "cards"}  # offered everything
    assert seen["workspace"] == "/workspace"


def test_a_hook_that_raises_does_not_fail_the_exec(monkeypatch):
    """It runs third-party serializers over agent data, so it will
    raise eventually. An exec must not fail because a chart could not
    be written."""
    def flatten(ui, workspace):
        raise RuntimeError("boom")

    _install(monkeypatch, flatten)
    g = {"ui": {"chart": object()}}
    runner._flatten_ui(g)
    assert set(g["ui"]) == {"chart"}


def test_a_hook_returning_none_is_tolerated(monkeypatch):
    """`-> set[str]` is the contract; a hook that forgets to return is
    a bug in the hook, not grounds for taking down the exec."""
    _install(monkeypatch, lambda ui, workspace: None)
    g = {"ui": {"chart": object()}}
    runner._flatten_ui(g)
    assert set(g["ui"]) == {"chart"}


def test_a_module_without_flatten_is_absent(monkeypatch):
    """Degrade to absent rather than raise — the same rule that keeps a
    reprobate shadow without `render` from failing print()."""
    _install(monkeypatch, None)
    assert runner._outputs_hook() is None


def test_empty_or_non_dict_ui_is_left_alone(monkeypatch):
    _install(monkeypatch, lambda ui, ws: {"anything"})
    for value in ({}, None, "not a dict", 7):
        g = {"ui": value}
        runner._flatten_ui(g)
        assert g["ui"] == value


# ---- resolution hygiene -------------------------------------------------


def test_extension_points_ignore_workspace_files(tmp_path, monkeypatch):
    """The property both extension points share, and the reason they
    resolve through one helper.

    cwd is the workspace and `python -m` puts cwd on sys.path, so a
    file an agent wrote can otherwise shadow the real package — which
    would put agent-authored code inside dud's own print and output
    paths.
    """
    (tmp_path / "dud_outputs.py").write_text("def flatten(ui, ws):\n    return set(ui)\n")
    (tmp_path / "reprobate.py").write_text("def render(obj, budget=0):\n    return 'pwned'\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DUD_WORKSPACE", str(tmp_path))
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "dud_outputs", raising=False)
    monkeypatch.delitem(sys.modules, "reprobate", raising=False)

    assert runner._from_image("dud_outputs", "flatten") is None
    # reprobate is a real dev dependency, so the check is that the
    # SHADOW did not win rather than that nothing resolved.
    resolved = runner._from_image("reprobate", "render")
    assert resolved is None or resolved(object()) != "pwned"


def test_resolution_restores_sys_path(monkeypatch):
    """It mutates sys.path to strip the workspace; user code runs after
    it and must see the path it started with."""
    monkeypatch.delitem(sys.modules, "dud_outputs", raising=False)
    before = list(sys.path)
    runner._from_image("dud_outputs", "flatten")
    assert sys.path == before
