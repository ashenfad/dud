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
    runner._RENDERERS.clear()
    yield
    runner._OUTPUTS_HOOKS.clear()
    runner._RENDERERS.clear()


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


# ---- the renderer's fallback chain --------------------------------------
#
# The asymmetry with the outputs hook is deliberate. dud DEFINES the
# render contract — render(obj, budget) exists because render_budget
# does — so it can ship a default implementation of it. The outputs
# hook gets no default, because there any default would be somebody's
# convention. Rule: name a default when dud defines the operation;
# require the caller to name one when the consumer defines the meaning.


def test_a_named_renderer_wins_over_the_default(monkeypatch):
    _install(monkeypatch, None)  # the package exists...
    sys.modules["acme_outputs.hooks"].render = lambda obj, budget=0: "mine"
    monkeypatch.setattr(runner, "_DEFAULT_RENDERER", ("reprobate", "render"))
    render = runner._renderer("acme_outputs.hooks:render")
    assert render(object()) == "mine"


def test_an_unnamed_renderer_falls_back_to_the_default(monkeypatch):
    _install(monkeypatch, None, "fallback_pkg.r")
    sys.modules["fallback_pkg.r"].render = lambda obj, budget=0: "default"
    monkeypatch.setattr(runner, "_DEFAULT_RENDERER", ("fallback_pkg.r", "render"))
    assert runner._renderer(None)(object()) == "default"


def test_a_missing_named_renderer_still_renders(monkeypatch):
    """The chain continues rather than dropping to plain str: each step
    is a real improvement on the next, and which one is live is a
    diagnostic (ping) rather than something an agent's output should
    turn on."""
    _install(monkeypatch, None, "fallback_pkg.r")
    sys.modules["fallback_pkg.r"].render = lambda obj, budget=0: "default"
    monkeypatch.setattr(runner, "_DEFAULT_RENDERER", ("fallback_pkg.r", "render"))
    assert runner._renderer("nope.missing:render")(object()) == "default"


def test_no_renderer_at_all_is_none(monkeypatch):
    monkeypatch.setattr(runner, "_DEFAULT_RENDERER", ("nope_absent", "render"))
    assert runner._renderer(None) is None
    assert runner._renderer("also.missing:render") is None


# ---- hijack resistance --------------------------------------------------


def test_a_preloaded_workspace_module_cannot_satisfy_a_hook(tmp_path,
                                                            monkeypatch):
    """Stripping sys.path is not enough on its own.

    Agent code runs BEFORE the harvest, so it can import its own
    shadow first; `import_module` then returns it straight out of
    sys.modules without consulting sys.path at all. Measured on a real
    guest before the fix: an agent that wrote `agentmod.py` into its
    workspace and imported it had its function called in place of the
    configured hook, and every binding it chose to drop was dropped.
    """
    shadow = tmp_path / "agent_hooks.py"
    shadow.write_text("def flatten(bindings, ws):\n    bindings.clear()\n"
                      "    return set()\n")
    monkeypatch.setenv("DUD_WORKSPACE", str(tmp_path))
    monkeypatch.syspath_prepend(str(tmp_path))
    # The agent got there first: it is already in sys.modules.
    import importlib.util

    spec = importlib.util.spec_from_file_location("agent_hooks", shadow)
    preloaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(preloaded)
    monkeypatch.setitem(sys.modules, "agent_hooks", preloaded)
    assert runner._from_image("agent_hooks", "flatten",
                              workspace=str(tmp_path)) is None
    harvest = {"keep": 1}
    assert runner._offer_outputs(harvest, "agent_hooks:flatten") == harvest


def test_a_module_outside_the_workspace_still_resolves(tmp_path, monkeypatch):
    """The origin check must not refuse the image. It is scoped to the
    workspace, not to cwd — on the subprocess rung the supervisor
    inherits the host's cwd, and treating that as agent-writable
    refused every module in the project's own virtualenv."""
    monkeypatch.setenv("DUD_WORKSPACE", str(tmp_path / "workspace"))
    _install(monkeypatch, lambda b, ws: {"gone"})
    assert runner._offer_outputs({"gone": 1, "kept": 2},
                                 SPEC) == {"kept": 2}


# ---- failure isolation --------------------------------------------------


def test_a_hook_that_rewrites_then_raises_changes_nothing(monkeypatch):
    """"Treated as having handled nothing" has to be true of the
    bindings too. A hook that rewrote one and blew up on the next used
    to have its half-finished edits kept, because the fallback returned
    the very dict it had been mutating."""
    def flatten(bindings, workspace):
        bindings["a"] = "clobbered"
        del bindings["b"]
        raise RuntimeError("failed on the next one")

    _install(monkeypatch, flatten)
    harvest = {"a": 1, "b": 2}
    assert runner._offer_outputs(harvest, SPEC) == {"a": 1, "b": 2}
    assert harvest == {"a": 1, "b": 2}  # the caller's dict, untouched
