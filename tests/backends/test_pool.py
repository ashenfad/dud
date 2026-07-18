"""VmPool logic with faked VMs, and reset_guest over the real rung-1 guest."""

from __future__ import annotations

from dud.backends import pool as poolmod


class FakeVM:
    """Just enough VfkitSession surface for the pool. The signature
    mirrors the real one so fingerprint normalization (defaults filled
    for sparse call-site kwargs) is actually exercised."""

    booted = 0

    def __init__(self, image="python:3.12-slim", arch=None, workspace="/workspace",
                 kernel=None, memory_mib=2048, cpus=2, home=None,
                 boot_timeout=30.0, packages=None, host_objects=None,
                 allow=None, cache=None, on_emit=None):
        FakeVM.booted += 1
        self._pool = None
        self._pool_kwargs = {
            "image": image, "arch": arch, "workspace": workspace,
            "kernel": kernel, "memory_mib": memory_mib, "cpus": cpus,
            "home": home, "packages": packages,
        }
        self.cache = cache if cache is not None else {}
        self.host_objects = host_objects or {}
        self.allow = allow or {}
        self.on_emit = on_emit
        self.emits = []
        self._closed = False
        self.requests: list[str] = []
        self.dead = False
        self.torn_down = False
        outer = self

        class Ch:
            def request(self, verb, body=None, bins=None):
                if outer.dead:
                    raise ConnectionError("vm died")
                outer.requests.append(verb)
                return {}, []

        self._ch = Ch()

    def ping(self):
        if self.dead:
            raise ConnectionError("vm died")
        return {"pong": True}

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._pool is not None:
            self._pool.release(self)
            return
        self.torn_down = True


def _pool(monkeypatch, **kw):
    monkeypatch.setattr(poolmod, "VfkitSession", FakeVM)
    FakeVM.booted = 0
    return poolmod.VmPool(**kw)


def test_close_parks_and_next_acquire_reuses(monkeypatch):
    p = _pool(monkeypatch)
    a = p.acquire(image="x", cache={"k": b"1"})
    assert FakeVM.booted == 1
    a.close()
    assert a.requests == ["reset_guest"]  # hygiene on release

    b = p.acquire(image="x", cache={"other": b"2"})
    assert b is a and FakeVM.booted == 1  # same VM, no second boot
    assert b.cache == {"other": b"2"}  # host state rebound
    assert b.emits == [] and not b._closed


def test_different_fingerprints_do_not_share(monkeypatch):
    p = _pool(monkeypatch)
    a = p.acquire(image="x", packages=["numpy"])
    a.close()
    b = p.acquire(image="x", packages=["numpy", "pandas"])
    assert b is not a and FakeVM.booted == 2


def test_sparse_and_default_kwargs_share_a_fingerprint(monkeypatch):
    """The bug the live test caught: release parks under fully-captured
    kwargs, acquire arrives with sparse ones — defaults must normalize
    to the same key or every reuse misses."""
    p = _pool(monkeypatch)
    a = p.acquire(memory_mib=2048)  # sparse
    a.close()  # parks under a's fully-captured _pool_kwargs
    b = p.acquire(image="python:3.12-slim", memory_mib=2048)  # explicit default
    assert b is a and FakeVM.booted == 1


def test_binding_kwargs_are_not_identity(monkeypatch):
    p = _pool(monkeypatch)
    a = p.acquire(image="x", host_objects={"db": object()})
    a.close()
    b = p.acquire(image="x", host_objects={"other": object()})
    assert b is a  # host_objects differ, VM identity doesn't


def test_failed_reset_tears_down_instead_of_parking(monkeypatch):
    p = _pool(monkeypatch)
    a = p.acquire(image="x")
    a.dead = True
    a.close()
    assert a.torn_down is True  # not parked: reset failed, VM shut down
    b = p.acquire(image="x")
    assert b is not a and FakeVM.booted == 2


def test_dead_parked_vm_is_replaced_on_acquire(monkeypatch):
    p = _pool(monkeypatch)
    a = p.acquire(image="x")
    a.close()
    a.dead = True  # dies while parked
    b = p.acquire(image="x")
    assert b is not a and FakeVM.booted == 2


def test_max_idle_evicts_overflow(monkeypatch):
    p = _pool(monkeypatch, max_idle=1)
    a = p.acquire(image="x")
    b = p.acquire(image="x")
    assert FakeVM.booted == 2
    a.close()
    b.close()  # bucket full: the older parked VM is torn down
    assert a.torn_down or b.torn_down


def test_ttl_expires_parked_vms(monkeypatch):
    p = _pool(monkeypatch, ttl=0.0)
    a = p.acquire(image="x")
    a.close()
    b = p.acquire(image="x")  # lazy expiry runs first: a is stale
    assert b is not a and FakeVM.booted == 2


def test_reset_guest_over_real_guest():
    """rung-1 integration: exports and files vanish, cwd resets."""
    from dud import Session

    with Session() as s:
        s.shell("export LEAKY=secret && mkdir -p d && echo x > d/f.txt && cd d")
        s._ch.request("reset_guest")
        r = s.shell("echo ${LEAKY:-unset}; ls; pwd")
        assert "unset" in r.transcript
        assert "f.txt" not in r.transcript
        assert r.cwd.endswith("/work")


def _no_auto(p):
    """Disable async auto-refill so boot counts are deterministic."""
    p._maybe_refill = lambda key: None
    return p


def _key(**kwargs):
    from dud.backends.pool import _fingerprint
    return _fingerprint(kwargs)


def test_prewarm_boots_and_parks(monkeypatch):
    p = _no_auto(_pool(monkeypatch))
    p.prewarm(2, background=False, image="x")
    assert FakeVM.booted == 2
    a = p.acquire(image="x")
    assert FakeVM.booted == 2  # served warm, no boot
    assert a.requests == []  # prewarmed VMs are pristine, no reset needed


def test_prewarm_refills_after_drain(monkeypatch):
    p = _no_auto(_pool(monkeypatch))
    p.prewarm(1, background=False, image="x")
    a = p.acquire(image="x")  # drains the warm level
    p._refill(_key(image="x"))  # what auto-refill runs in the background
    assert FakeVM.booted == 2  # a's replacement is parked
    b = p.acquire(image="x")
    assert b is not a and FakeVM.booted == 2  # warm again


def test_prewarm_target_survives_ttl(monkeypatch):
    p = _no_auto(_pool(monkeypatch, ttl=0.0))
    p.prewarm(1, background=False, image="x")
    b = p.acquire(image="x")  # ttl=0 would have expired an untargeted VM
    assert FakeVM.booted == 1  # served the prewarmed VM, no fresh boot
    assert b.requests == []


def test_prewarm_target_raises_release_limit(monkeypatch):
    p = _no_auto(_pool(monkeypatch, max_idle=1))
    p.prewarm(3, background=False, image="x")
    assert FakeVM.booted == 3  # target beats max_idle for its own key


def test_acquire_kicks_background_refill(monkeypatch):
    """The auto-refill hook fires on drain (thread mechanics faked out)."""
    p = _pool(monkeypatch)
    kicks = []
    p._maybe_refill = lambda key: kicks.append(key)
    p.prewarm(1, background=False, image="x")
    p.acquire(image="x")
    assert kicks == [_key(image="x")]
