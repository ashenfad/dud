"""The public API surface: front door, exception spine, ergonomics."""

from __future__ import annotations

import pytest

import dud
from dud.errors import DudError


def test_lazy_exports_resolve():
    assert dud.SessionLost is not None
    assert dud.IsolationUnavailable is not None
    assert dud.VfkitSession is not None
    assert dud.scratch_master is not None
    with pytest.raises(AttributeError):
        dud.not_a_thing


def test_public_errors_share_the_spine():
    from dud.images.debs import DebError
    from dud.images.registry import RegistryError
    from dud.images.scratch import ScratchError
    from dud.kernels import KernelFetchError

    for exc in (dud.SessionLost, dud.IsolationUnavailable,
                dud.NotRepresentable, dud.ProtocolError, dud.RemoteError,
                DebError, RegistryError, ScratchError, KernelFetchError):
        assert issubclass(exc, DudError), exc
    # Historical bases survive: existing except clauses keep working.
    assert issubclass(dud.SessionLost, RuntimeError)
    assert issubclass(dud.NotRepresentable, ValueError)


def test_session_factory_subprocess_roundtrip():
    with dud.session() as s:
        r = s.shell("echo front-door")
        assert r  # __bool__ is exit_code == 0
        assert "front-door" in r.transcript


def test_session_factory_rejects_bad_combinations():
    with pytest.raises(ValueError, match="VM-rung"):
        dud.session("subprocess", pooled=True)
    with pytest.raises(ValueError, match="pooled=True"):
        dud.session("vfkit", state="commit-abc")
    with pytest.raises(ValueError, match="unknown backend"):
        dud.session("qemu")


def test_session_factory_vfkit_paths(monkeypatch):
    import dud.backends.pool as poolmod
    import dud.backends.vfkit as vfkitmod

    built = []
    monkeypatch.setattr(vfkitmod, "VfkitSession",
                        lambda **kw: built.append(("direct", kw)) or "direct")

    class FakePool:
        def acquire(self, state=None, **kw):
            built.append(("pooled", state, kw))
            return "pooled"

    monkeypatch.setattr(poolmod, "shared_pool",
                        lambda cls=None: FakePool())
    assert dud.session("vfkit", image="x") == "direct"
    assert dud.session("vm", pooled=True, state="c1", image="x") == "pooled"
    assert built == [("direct", {"image": "x"}),
                     ("pooled", "c1", {"image": "x"})]


def test_python_result_truthiness():
    from dud.results import ExecError, PythonResult

    assert PythonResult(ok=True, transcript="")
    assert not PythonResult(ok=False, transcript="",
                            error=ExecError("E", "boom"))


def test_scratch_master_keys_are_stable_and_collision_free(tmp_path, monkeypatch):
    from dud.images import scratch

    def fake_blank(size_mib, home=None, arch=None):
        p = tmp_path / f"blank-{size_mib}"
        p.write_bytes(b"blank")
        return p

    monkeypatch.setattr(scratch, "blank_ext4", fake_blank)
    a = scratch.scratch_master("app/token:1", home=tmp_path)
    assert a.exists() and a.read_bytes() == b"blank"
    assert scratch.scratch_master("app/token:1", home=tmp_path) == a
    # Same sanitized form, different raw keys -> different masters.
    b = scratch.scratch_master("app_token_1", home=tmp_path)
    assert b != a
    with pytest.raises(scratch.ScratchError):
        scratch.scratch_master("", home=tmp_path)


def test_session_names_its_extension_points():
    """The seam has to be discoverable from the seam.

    Everything a consumer configures used to live in `**kwargs: Any` —
    invisible to `help()`, to autocomplete and to a type checker. For a
    library whose current job is finding the right API seams, the one
    blessed entry point teaching nothing was the weakest of them, and
    `outputs_hook` was moved onto the session precisely so the
    mechanism would show up in a signature.
    """
    import inspect
    import typing

    params = inspect.signature(dud.session).parameters
    for name in ("host_objects", "allow", "cache", "on_emit",
                 "outputs_hook", "render_hook",
                 "image", "packages", "memory_mib"):
        assert name in params, f"{name} is still hidden in **kwargs"
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY

    # And the annotations must actually resolve — `Callable` was
    # referenced before it was imported, which `from __future__ import
    # annotations` hides until something asks.
    hints = typing.get_type_hints(dud.session)
    assert hints["on_emit"] is not None
    assert "kwargs" in params  # the long tail still passes through


def test_session_forwards_only_what_the_caller_named(monkeypatch):
    """`image=None` means "unspecified", not "no image".

    Naming a kwarg whose backend default is a real value makes this a
    live hazard: forwarding the None would override
    `python:3.12-slim` with nothing.
    """
    seen = {}

    class FakeSession:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr("dud.backends.vfkit.VfkitSession", FakeSession)
    dud.session("vfkit", outputs_hook="pkg:fn")
    assert seen == {"outputs_hook": "pkg:fn"}, seen
    assert "image" not in seen and "memory_mib" not in seen


def test_session_still_passes_the_long_tail_through(monkeypatch):
    """Naming the discoverable ones must not amputate the rest."""
    seen = {}

    class FakeSession:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr("dud.backends.vfkit.VfkitSession", FakeSession)
    dud.session("vfkit", medium="erofs", cpus=4, image="node:22")
    assert seen == {"medium": "erofs", "cpus": 4, "image": "node:22"}
