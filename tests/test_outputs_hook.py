"""The outputs hook: dud offers the bindings, the image shapes them.

dud used to carry this itself, in a guest module that knew plotly
figures serialize to `ui/<name>.plotly.json`, that DataFrames get
`head(200)` — and that the dict holding them was called `ui`. Formats
and vocabulary both belong to whoever consumes the diff, so both moved
into a package the image supplies, named by the caller.

What is tested here is dud's half: resolve what the caller named, offer
it everything, respect what it claims, survive it misbehaving, and
never resolve it from files the agent wrote.
"""

from __future__ import annotations

import sys
import types

import pytest

from dud.guest import runner

SPEC = "acme_outputs.hooks:flatten"


@pytest.fixture(autouse=True)
def _unresolved():
    """Hooks cache per spec, and the fork template outlives one exec;
    tests need each case resolved fresh."""
    runner._OUTPUTS_HOOKS.clear()
    runner._RENDERER = None
    yield
    runner._OUTPUTS_HOOKS.clear()
    runner._RENDERER = None


def _install(monkeypatch, flatten, module: str = "acme_outputs.hooks"):
    """Stand in for a package the image layered in."""
    pkg_name = module.split(".")[0]
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = []  # a package, so the submodule import resolves
    mod = types.ModuleType(module)
    if flatten is not None:
        mod.flatten = flatten
    monkeypatch.setitem(sys.modules, pkg_name, pkg)
    monkeypatch.setitem(sys.modules, module, mod)
    return mod


# ---- the spec ----------------------------------------------------------


def test_spec_splits_module_from_attribute():
    assert runner.split_hook_spec("pkg.mod:fn") == ("pkg.mod", "fn")
    assert runner.split_hook_spec("mod:fn") == ("mod", "fn")


def test_a_dotted_path_without_a_colon_is_rejected():
    """`a.b.c` cannot say whether `b` is a module or an attribute, so
    guessing would make a bad package name and a bad function name fail
    the same unhelpful way."""
    for bad in ("pkg.mod.fn", "pkg.mod:", ":fn", ""):
        with pytest.raises(ValueError, match="pkg.module:function"):
            runner.split_hook_spec(bad)


def test_a_malformed_spec_is_absent_not_an_exception():
    """It reaches the runner from a caller's config, so a bad one must
    degrade like any other missing hook — ping is where it is visible."""
    assert runner._outputs_hook("nonsense") is None


# ---- offering the bindings ---------------------------------------------


def test_no_hook_configured_means_nothing_is_flattened():
    """The zero-knowledge default. A caller who names no hook gets
    unrepresentable values reported in outputs_skipped, not invented
    files in their workspace."""
    harvest = {"chart": object()}
    assert runner._offer_outputs(harvest, None) == harvest


def test_a_named_but_missing_hook_is_not_an_error():
    """An exec whose hook failed to import behaves exactly like one
    with no hook. Loudness belongs in ping(), not in every exec."""
    harvest = {"chart": object()}
    assert runner._offer_outputs(harvest, "nope.missing:flatten") == harvest


def test_handled_names_are_dropped_and_the_rest_crosses(monkeypatch):
    """The partition: what the hook claims is removed from the harvest,
    and the rest still crosses to the host."""
    seen = {}

    def flatten(bindings, workspace):
        seen["bindings"], seen["workspace"] = dict(bindings), workspace
        return {"chart"}

    _install(monkeypatch, flatten)
    monkeypatch.setenv("DUD_WORKSPACE", "/workspace")
    out = runner._offer_outputs({"chart": object(), "n": 7}, SPEC)
    assert set(out) == {"n"}
    assert set(seen["bindings"]) == {"chart", "n"}  # offered everything
    assert seen["workspace"] == "/workspace"


def test_dud_names_no_binding(monkeypatch):
    """The hook sees every top-level binding, not one dud picked. An
    earlier cut passed only a binding literally named `ui`, which moved
    the formats out while leaving the vocabulary in."""
    seen = {}
    _install(monkeypatch, lambda b, ws: seen.update(b) or set())
    runner._offer_outputs({"fig": object(), "ui": {}, "whatever": 1}, SPEC)
    assert set(seen) == {"fig", "ui", "whatever"}


def test_a_hook_may_rewrite_a_binding_in_place(monkeypatch):
    """How a `ui = {...}`-style convention is expressed now: write some
    of the dict to files, put back the remainder, claim nothing at the
    top level. dud never learns the word."""
    def flatten(bindings, workspace):
        ui = bindings.get("ui")
        if isinstance(ui, dict):
            bindings["ui"] = {k: v for k, v in ui.items() if k != "chart"}
        return set()

    _install(monkeypatch, flatten)
    out = runner._offer_outputs({"ui": {"chart": object(), "n": 1}}, SPEC)
    assert out["ui"] == {"n": 1}


def test_a_hook_that_raises_does_not_fail_the_exec(monkeypatch):
    """It runs third-party serializers over agent data, so it will
    raise eventually. An exec must not fail because a chart could not
    be written."""
    def flatten(bindings, workspace):
        raise RuntimeError("boom")

    _install(monkeypatch, flatten)
    harvest = {"chart": object()}
    assert runner._offer_outputs(harvest, SPEC) == harvest


def test_a_hook_returning_none_is_tolerated(monkeypatch):
    """`-> set[str]` is the contract; a hook that forgets to return is
    a bug in the hook, not grounds for taking down the exec. Note which
    way this fails: nothing is dropped."""
    _install(monkeypatch, lambda bindings, workspace: None)
    harvest = {"chart": object()}
    assert runner._offer_outputs(harvest, SPEC) == harvest


def test_a_module_without_the_named_function_is_absent(monkeypatch):
    """Degrade to absent rather than raise — the same rule that keeps a
    reprobate shadow without `render` from failing print()."""
    _install(monkeypatch, None)
    assert runner._outputs_hook(SPEC) is None


def test_an_empty_harvest_never_reaches_the_hook(monkeypatch):
    """An exec that bound nothing has nothing to offer; don't pay for
    an import, and don't hand a hook an empty dict to reason about."""
    called = []
    _install(monkeypatch, lambda b, ws: called.append(1) or {"x"})
    assert runner._offer_outputs({}, SPEC) == {}
    assert not called


def test_hooks_cache_per_spec(monkeypatch):
    """The fork template outlives one exec and a pooled VM can be
    rebound to a caller naming a different hook, so one cached
    resolution must not answer for another spec."""
    _install(monkeypatch, lambda b, ws: {"a"}, "acme_outputs.hooks")
    _install(monkeypatch, lambda b, ws: {"b"}, "other_pkg.out")
    assert set(runner._offer_outputs({"a": 1, "b": 2}, SPEC)) == {"b"}
    assert set(runner._offer_outputs({"a": 1, "b": 2},
                                     "other_pkg.out:flatten")) == {"a"}


# ---- resolution hygiene -------------------------------------------------


def test_extension_points_ignore_workspace_files(tmp_path, monkeypatch):
    """The property both extension points share, and the reason they
    resolve through one helper.

    cwd is the workspace and `python -m` puts cwd on sys.path, so a
    file an agent wrote can otherwise shadow the real package — which
    would put agent-authored code inside dud's own print and output
    paths.
    """
    (tmp_path / "agent_hooks.py").write_text(
        "def flatten(bindings, ws):\n    return set(bindings)\n"
    )
    (tmp_path / "reprobate.py").write_text(
        "def render(obj, budget=0):\n    return 'pwned'\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DUD_WORKSPACE", str(tmp_path))
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "agent_hooks", raising=False)
    monkeypatch.delitem(sys.modules, "reprobate", raising=False)

    assert runner._from_image("agent_hooks", "flatten") is None
    # reprobate is a real dev dependency, so the check is that the
    # SHADOW did not win rather than that nothing resolved.
    resolved = runner._from_image("reprobate", "render")
    assert resolved is None or resolved(object()) != "pwned"


def test_resolution_restores_sys_path(monkeypatch):
    """It mutates sys.path to strip the workspace; user code runs after
    it and must see the path it started with."""
    monkeypatch.delitem(sys.modules, "acme_outputs", raising=False)
    before = list(sys.path)
    runner._from_image("acme_outputs", "flatten")
    assert sys.path == before
