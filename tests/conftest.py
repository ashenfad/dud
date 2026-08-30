import os

import pytest

# Conformance VMs are slim-python guests, not DS images: 1 GiB is
# comfortable and halves the churn that wears out the nested-virt dev
# VM (2 GiB allocs + 2 GiB snapshot writes per freeze — see
# dev/fc-test.sh for the other half of that story). Override per test
# with an explicit memory_mib kwarg.
_TEST_VM_MIB = int(os.environ.get("DUD_TEST_VM_MIB", "1024"))


# Reuse VMs across tests instead of booting one per test.
#
# The corpus is ~120 tests whose actual work is milliseconds — a python
# exec is ~25 ms — against a VM boot that is ~1 s on vfkit and ~8 s on
# firecracker under nested virt. So the suite has been measuring boot,
# ~120 times, and the assertions rode along.
#
# `close()` on a pooled session parks it rather than powering it off,
# and the next acquire resets the guest (trees wiped, boot env
# restored, stray processes killed — see Supervisor.do_reset_guest) and
# hands the same machine back. Tests that need a genuinely cold machine
# say so with @pytest.mark.cold_boot.
#
# Off by default on purpose: pooled reuse is what a consumer does, but
# a cold boot per test is the stricter thing to be testing, and the CI
# job that gates merges should keep proving a VM can boot and serve
# from nothing. Turned on where the wall clock matters.
_POOLED = os.environ.get("DUD_TEST_POOL") == "1"

_VM_BACKENDS = {"vfkit": ("dud.backends.vfkit", "VfkitSession"),
                "firecracker": ("dud.backends.firecracker", "FirecrackerSession")}


def _new_session(cold: bool = False, **kwargs):
    """Construct the backend selected by ``DUD_BACKEND`` (default subprocess).

    The conformance suite is one corpus over every rung: it builds sessions
    only through this factory (or the ``session`` fixture), so the same test
    bodies validate subprocess and vfkit unchanged. Backends share the
    common kwargs (host_objects/allow/cache/on_emit).

    ``cold`` forces a fresh boot even under ``DUD_TEST_POOL``.
    """
    backend = os.environ.get("DUD_BACKEND", "subprocess")
    if backend in _VM_BACKENDS:
        import importlib

        mod, name = _VM_BACKENDS[backend]
        cls = getattr(importlib.import_module(mod), name)
        # DUD_MEDIUM lets the same corpus run against an erofs root
        # (DUD_BACKEND=vfkit DUD_MEDIUM=erofs uv run pytest tests/conformance)
        kwargs.setdefault("medium", os.environ.get("DUD_MEDIUM", "initramfs"))
        kwargs.setdefault("memory_mib", _TEST_VM_MIB)
        if _POOLED and not cold:
            from dud.backends.pool import shared_pool

            return shared_pool(cls).acquire(**kwargs)
        return cls(**kwargs)
    if backend == "subprocess":
        from dud import Session
        return Session(**kwargs)
    raise ValueError(f"unknown DUD_BACKEND {backend!r}")


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "cold_boot: needs a freshly booted machine, never a pooled one",
    )


@pytest.fixture
def make_session(request):
    """Factory fixture: open sessions on the configured backend; auto-close."""
    cold = request.node.get_closest_marker("cold_boot") is not None
    created = []

    def factory(**kwargs):
        s = _new_session(cold=cold, **kwargs)
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
