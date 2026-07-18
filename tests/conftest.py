import os

import pytest


def _new_session(**kwargs):
    """Construct the backend selected by ``DUD_BACKEND`` (default subprocess).

    The conformance suite is one corpus over every rung: it builds sessions
    only through this factory (or the ``session`` fixture), so the same test
    bodies validate subprocess and vfkit unchanged. Backends share the
    common kwargs (host_objects/allow/cache/on_emit).
    """
    backend = os.environ.get("DUD_BACKEND", "subprocess")
    if backend == "vfkit":
        from dud.backends.vfkit import VfkitSession
        return VfkitSession(**kwargs)
    if backend == "subprocess":
        from dud import Session
        return Session(**kwargs)
    raise ValueError(f"unknown DUD_BACKEND {backend!r}")


@pytest.fixture
def make_session():
    """Factory fixture: open sessions on the configured backend; auto-close."""
    created = []

    def factory(**kwargs):
        s = _new_session(**kwargs)
        created.append(s)
        return s

    yield factory
    for s in created:
        s.close()


@pytest.fixture
def session(make_session):
    """The conformance seam: everything tested through this fixture is the
    guest contract, backend-agnostic. VM rungs parameterize via DUD_BACKEND."""
    return make_session()
