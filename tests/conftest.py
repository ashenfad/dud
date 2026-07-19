import os

import pytest

# Conformance VMs are slim-python guests, not DS images: 1 GiB is
# comfortable and halves the churn that wears out the nested-virt dev
# VM (2 GiB allocs + 2 GiB snapshot writes per freeze — see
# dev/fc-test.sh for the other half of that story). Override per test
# with an explicit memory_mib kwarg.
_TEST_VM_MIB = int(os.environ.get("DUD_TEST_VM_MIB", "1024"))


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
        # DUD_MEDIUM lets the same corpus run against an erofs root
        # (DUD_BACKEND=vfkit DUD_MEDIUM=erofs uv run pytest tests/conformance)
        kwargs.setdefault("medium", os.environ.get("DUD_MEDIUM", "initramfs"))
        kwargs.setdefault("memory_mib", _TEST_VM_MIB)
        return VfkitSession(**kwargs)
    if backend == "firecracker":
        from dud.backends.firecracker import FirecrackerSession
        kwargs.setdefault("medium", os.environ.get("DUD_MEDIUM", "initramfs"))
        kwargs.setdefault("memory_mib", _TEST_VM_MIB)
        return FirecrackerSession(**kwargs)
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
