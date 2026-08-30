"""VmPool logic with faked VMs, and reset_guest over the real rung-1 guest."""

from __future__ import annotations

import pathlib
import tempfile
import types

import pytest

from dud.backends import golden as goldenmod
from dud.backends import pool as poolmod


@pytest.fixture(autouse=True)
def _hermetic_golden_store(monkeypatch, tmp_path):
    """No test in this file may see the developer's real ~/.dud.

    Not hygiene for its own sake — it was a live source of
    local-passes/CI-fails. A machine that already holds a golden takes
    the clone branch where a bare CI runner takes the boot-and-seed
    branch, so the same test counts different boots depending on whose
    laptop it runs on.
    """
    monkeypatch.setattr(goldenmod, "dud_home", lambda: tmp_path)


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
        self.bodies: list[dict] = []
        self.park_state = None
        self.resumed = False
        self.dead = False
        self.torn_down = False
        self._in_flight = 0
        self.last_used = 0.0
        self._scratch_master = "fake-master"  # truthy: promotion armed
        self.promotions = 0
        outer = self

        class Ch:
            def request(self, verb, body=None, bins=None):
                if outer.dead:
                    raise ConnectionError("vm died")
                outer.requests.append(verb)
                outer.bodies.append(body or {})
                return {}, []

        self._ch = Ch()

    def _request(self, verb, body=None, bins=None):
        """The real HostSession's wire seam, which the pool goes through
        so lifecycle verbs get a deadline like every other request."""
        return self._ch.request(verb, body, bins)

    def ping(self):
        if self.dead:
            raise ConnectionError("vm died")
        return {"pong": True}

    def promote_scratch(self):
        # Mirrors the real guard: teardown disarms by clearing the master.
        if self._scratch_master is not None:
            self.promotions += 1

    def close(self, park_state=None):
        # Mirrors VmSession.close: stamping the tag through close() is
        # the documented way to park with affinity, so the fake has to
        # accept it or tests silently exercise a different API.
        if park_state is not None:
            self.park_state = park_state
        if self._closed:
            return
        self._closed = True
        if self._pool is not None:
            self._pool.release(self)
            return
        self.torn_down = True


def _pool(monkeypatch, **kw):
    # The pool asks _default_cls() which rung this host runs; the
    # fake stands in for whichever that is.
    monkeypatch.setattr(poolmod, "_default_cls", lambda: FakeVM)
    FakeVM.booted = 0
    # Off by default here so boot counts are deterministic: automatic
    # seeding boots a second machine on a background thread, which
    # would race every `booted ==` assertion in this file. Tests about
    # seeding pass auto_seed=True.
    kw.setdefault("auto_seed", False)
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
    a = p.acquire(image="x", host_objects={"db": object()},
                  allow={"db": set()})
    a.close()
    b = p.acquire(image="x", host_objects={"other": object()},
                  allow={"other": set()})
    assert b is a  # host_objects differ, VM identity doesn't


def test_allowlist_does_not_lapse_on_a_pool_hit(monkeypatch):
    """Reuse rebinds host_objects/allow straight onto a parked session,
    bypassing the constructor. A fail-closed default that holds on a
    fresh boot and lapses on a warm one is worse than none."""
    import pytest

    from dud.errors import PolicyError

    p = _pool(monkeypatch)
    a = p.acquire(image="x", host_objects={"db": object()},
                  allow={"db": {"query"}})
    a.close()  # parks it
    with pytest.raises(PolicyError):
        p.acquire(image="x", host_objects={"db": object()})
    # The parked VM stays parked: a refused acquire hands out nothing.
    b = p.acquire(image="x", host_objects={"db": object()},
                  allow={"db": {"query"}})
    assert b is a and FakeVM.booted == 1


def test_rebind_checks_at_the_assignment_point(monkeypatch):
    """Not merely via acquire: the guard sits where the fields land."""
    import pytest

    from dud.errors import PolicyError

    p = _pool(monkeypatch)
    a = p.acquire(image="x", host_objects={"db": object()},
                  allow={"db": set()})
    with pytest.raises(PolicyError):
        p._rebind(a, {"host_objects": {"db": object()}, "allow": None,
                      "cache": None, "on_emit": None})


def test_rebind_clears_a_lost_latch(monkeypatch):
    """A latch is per-session state that bypasses the constructor, so a
    stale one would make a perfectly live VM refuse its new owner. A
    genuinely lost session never reaches here — release() discards one
    whose reset failed — which is why this is worth pinning rather than
    assuming."""
    p = _pool(monkeypatch)
    a = p.acquire(image="x")
    a._lost = "guest lost during 'ping'"
    p._rebind(a, {"host_objects": None, "allow": None, "cache": None,
                  "on_emit": None, "outputs_hook": None, "render_hook": None})
    assert a._lost is None


def test_a_bad_allowlist_costs_nobody_a_vm(monkeypatch):
    """The check runs before _make_room, which under max_total can
    reclaim somebody else's session — no sense paying that for a config
    error that was going to raise regardless."""
    import pytest

    from dud.errors import PolicyError

    p = _no_auto(_pool(monkeypatch, max_total=1))
    held = p.acquire(image="x")
    with pytest.raises(PolicyError):
        p.acquire(image="y", host_objects={"db": object()})
    assert held.torn_down is False
    assert FakeVM.booted == 1


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


def test_park_promotes_scratch_disposal_never_does(monkeypatch):
    """The scratch contract's clean-path gate at the pool layer: a park
    (successful reset) promotes; every disposal path — TTL expiry,
    overflow, reclaim, failed reset — must NOT publish scratch (a
    reclaimed VM was never quiesced; a TTL victim's clone is staler
    than whatever parked since)."""
    p = _no_auto(_pool(monkeypatch, max_idle=1, max_total=None))
    a = p.acquire(image="x")
    a.close()  # park: promote exactly once
    assert a.promotions == 1

    b = p.acquire(image="x")  # note: reuses a (same object)
    c = p.acquire(image="x")
    b.close()
    parked = b.promotions  # every promotion so far was a legitimate park
    c.close()  # overflow: b (older) is torn down
    assert b.torn_down
    assert b.promotions == parked  # the eviction itself promoted nothing
    assert b._scratch_master is None  # disposal disarmed promotion

    # Failed reset: teardown instead of park, no promotion.
    d = p.acquire(image="fresh-z")  # new fingerprint: a pristine FakeVM
    d.dead = True
    d.close()
    assert d.torn_down and d.promotions == 0

    # Bound reclaim under max_total: never a promotion.
    p2 = _no_auto(_pool(monkeypatch, max_total=1))
    e = p2.acquire(image="x")
    p2.acquire(image="y")  # forces reclaim of bound LRU e
    assert e.torn_down and e.promotions == 0


def test_refill_respects_max_total(monkeypatch):
    p = _no_auto(_pool(monkeypatch, max_total=2))
    a = p.acquire(image="x")
    b = p.acquire(image="x")
    p.prewarm(2, background=False, image="x")  # cap full: no boots
    assert FakeVM.booted == 2
    a.close()
    b.close()
    p._refill(_key(image="x"))  # room now exists as idle slots drain in
    assert FakeVM.booted == 2  # still capped: bound+idle == max_total


def test_make_room_spares_prewarm_floor(monkeypatch):
    """Reclaiming a prewarm-target VM would just re-boot it (churn):
    the floor is exempt from idle-victim selection, so the cap
    over-boots instead."""
    p = _no_auto(_pool(monkeypatch, max_total=1))
    p.prewarm(1, background=False, image="x")
    warm = p._idle[_key(image="x")][0][2]
    p.acquire(image="y")  # needs room; the only idle VM is floored
    assert warm.torn_down is False
    assert FakeVM.booted == 2  # over-boot, cap as pressure valve


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


def test_prewarm_swallows_an_unbootable_environment(monkeypatch):
    """No kernel / no KVM / no vfkit: prewarming is an optimization, so
    the deficit simply goes unfilled."""
    from dud.errors import IsolationUnavailable

    class NoKvm(FakeVM):
        def __init__(self, **kw):
            raise IsolationUnavailable("/dev/kvm is not accessible")

    p = _no_auto(_pool(monkeypatch, session_cls=NoKvm))
    p.prewarm(1, background=False, image="x")  # returns quietly
    assert p._idle.get(_key(image="x"), []) == []


def test_prewarm_surfaces_a_boot_bug_instead_of_going_cold(monkeypatch):
    """A bad kwarg is a caller error, not a hostile environment. Letting
    it out beats a pool that stays silently cold forever — which is what
    a blind `except Exception` here used to buy."""
    import pytest

    class BadKwarg(FakeVM):
        def __init__(self, **kw):
            raise TypeError("unexpected keyword argument 'medum'")

    p = _no_auto(_pool(monkeypatch, session_cls=BadKwarg))
    with pytest.raises(TypeError):
        p.prewarm(1, background=False, image="x")
    assert p._filling == set()  # the fill claim is released either way


def test_bound_reclaim_warns_because_an_owner_pays_for_it(monkeypatch, caplog):
    """Reclaiming a *bound* VM makes somebody's next call raise
    SessionLost. Recoverable, but never something to work out by
    inference from a latency spike."""
    import logging

    p = _no_auto(_pool(monkeypatch, max_total=1))
    with caplog.at_level(logging.INFO, logger="dud.backends.pool"):
        a = p.acquire(image="x")
        p.acquire(image="y")  # forces reclaim of bound LRU `a`
    assert a.torn_down
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "SessionLost" in warnings[0].getMessage()


def test_idle_reclaim_is_only_informational(monkeypatch, caplog):
    """Nobody holds an idle VM, so its reclaim is news, not a warning."""
    import logging

    p = _no_auto(_pool(monkeypatch, max_total=1))
    a = p.acquire(image="x")
    a.close()  # parks it idle
    with caplog.at_level(logging.DEBUG, logger="dud.backends.pool"):
        p.acquire(image="y")  # reclaims the idle VM to stay under the cap
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []
    assert any("reclaiming an idle VM" in r.getMessage()
               for r in caplog.records)


def test_prewarm_unavailable_says_so(monkeypatch, caplog):
    """The silent-degradation case finding #7 was about: no KVM meant a
    permanently cold pool and not one word about why."""
    import logging

    from dud.errors import IsolationUnavailable

    class NoKvm(FakeVM):
        def __init__(self, **kw):
            raise IsolationUnavailable("/dev/kvm is not accessible")

    p = _no_auto(_pool(monkeypatch, session_cls=NoKvm))
    with caplog.at_level(logging.INFO, logger="dud.backends.pool"):
        p.prewarm(1, background=False, image="x")
    assert any("prewarm unavailable" in r.getMessage() and "kvm" in
               r.getMessage().lower() for r in caplog.records)


def test_library_attaches_no_handlers(monkeypatch):
    """Where records go is the embedder's call, so dud configures
    nothing and attaches nothing — it only ever emits.

    Not "the library is silent": WARNING and above still reach stderr
    through ``logging.lastResort`` when nobody has configured logging
    at all, which is stdlib behavior and the right default for the two
    warnings this module raises.
    """
    import logging

    p = _no_auto(_pool(monkeypatch, max_total=1))
    a = p.acquire(image="x")
    p.acquire(image="y")  # exercises the WARNING path
    a.close()
    for name in ("dud", "dud.backends.pool", "dud.images.builder",
                 "dud.images.registry"):
        lg = logging.getLogger(name)
        assert lg.handlers == [], f"{name} attached a handler"
        assert lg.propagate, f"{name} broke propagation to the embedder"
        assert lg.level == logging.NOTSET, f"{name} forced a level"


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


def test_state_affinity_resume_skips_wipe(monkeypatch):
    """Park tagged with a state -> a same-state acquire gets the SAME
    tree (keep_tree reset) and resumed=True; the owner skips its push."""
    p = _no_auto(_pool(monkeypatch))
    a = p.acquire(image="x")
    a.park_state = "commit-abc"
    a.close()
    assert a.requests == ["reset_guest"]
    assert a.bodies == [{"keep_tree": True}]  # tree kept in place
    assert a.park_state is None  # tags never survive a park cycle

    b = p.acquire(image="x", state="commit-abc")
    assert b is a and b.resumed is True


def test_state_mismatch_falls_back_untagged(monkeypatch):
    p = _no_auto(_pool(monkeypatch))
    a = p.acquire(image="x")
    a.park_state = "commit-abc"
    a.close()
    b = p.acquire(image="x", state="commit-OTHER")
    assert b is a  # still reused (push_tree will wipe+load)
    assert b.resumed is False


def test_untagged_park_wipes_and_never_resumes(monkeypatch):
    p = _no_auto(_pool(monkeypatch))
    a = p.acquire(image="x")
    a.close()  # no park_state stamped
    assert a.bodies == [{"keep_tree": False}]
    b = p.acquire(image="x", state="commit-abc")
    assert b is a and b.resumed is False


def test_affinity_prefers_match_over_older_vm(monkeypatch):
    p = _no_auto(_pool(monkeypatch, max_idle=2))
    a = p.acquire(image="x")
    b = p.acquire(image="x")
    a.park_state = "commit-A"
    a.close()
    b.park_state = "commit-B"
    b.close()  # b parked newest; a is the older entry
    got = p.acquire(image="x", state="commit-A")
    assert got is a and got.resumed is True


def test_acquire_prefers_most_recently_parked(monkeypatch):
    """MRU: the newest parked VM (hottest caches) serves next; the
    oldest idles toward TTL/reclaim."""
    p = _no_auto(_pool(monkeypatch, max_idle=2))
    a = p.acquire(image="x")
    b = p.acquire(image="x")
    a.close()
    b.close()  # b parked last = newest
    assert p.acquire(image="x") is b


def test_max_total_reclaims_idle_before_bound(monkeypatch):
    p = _no_auto(_pool(monkeypatch, max_total=2))
    a = p.acquire(image="x")
    b = p.acquire(image="x")
    a.close()  # a idle, b bound; total = 2 = cap
    c = p.acquire(image="y")  # new fingerprint: must boot -> needs room
    assert a.torn_down is True  # idle victim, owner-held b untouched
    assert b.torn_down is False
    assert c is not a


def test_max_total_reclaims_lru_bound_when_no_idle(monkeypatch):
    p = _no_auto(_pool(monkeypatch, max_total=2))
    a = p.acquire(image="x")
    b = p.acquire(image="x")
    a.last_used = 10.0  # quiet longest
    b.last_used = 20.0
    c = p.acquire(image="y")
    assert a.torn_down is True and b.torn_down is False
    assert c is not a
    # a's owner discovers the loss on next use, not via an exception here
    assert a._pool is None


def test_max_total_never_reclaims_mid_request(monkeypatch):
    p = _no_auto(_pool(monkeypatch, max_total=1))
    a = p.acquire(image="x")
    a._in_flight = 1  # mid-request: untouchable
    p.acquire(image="y")  # over-boots rather than blocking
    assert a.torn_down is False
    assert FakeVM.booted == 2


def test_release_and_teardown_clear_bound_registry(monkeypatch):
    p = _no_auto(_pool(monkeypatch))
    a = p.acquire(image="x")
    assert id(a) in p._bound
    a.close()  # parked: bound -> idle
    assert id(a) not in p._bound
    b = p.acquire(image="x")
    assert b is a and id(b) in p._bound


def test_reset_guest_keep_tree_over_real_guest():
    """keep_tree parks the workspace in place; env/cwd hygiene still runs."""
    from dud import Session

    with Session() as s:
        s.shell("export LEAKY=secret && echo keepme > f.txt && cd /")
        s._ch.request("reset_guest", {"keep_tree": True})
        r = s.shell("echo ${LEAKY:-unset}; cat f.txt; pwd")
        assert "unset" in r.transcript  # env reset
        assert "keepme" in r.transcript  # tree survived
        assert r.cwd.endswith("/work")  # cwd reset


# ---- frozen parking (firecracker posture, duck-typed) -----------------


class FrozenFakeVM(FakeVM):
    """A FakeVM that can freeze/thaw — the firecracker posture.

    The explicit signature matters: the pool fingerprints boot identity
    off ``inspect.signature(session_cls.__init__)``, exactly like the
    real session classes."""

    #: What this fake "booted from". The real session resolves these
    #: before it looks at restore_from; tests move them to stand for an
    #: upgraded dud or an image tag that now resolves to new bytes.
    rootfs = "/artifacts/rootfs-aaaaaaaa"
    kernel_image = "/kernels/amd64/Image"

    def __init__(self, image="python:3.12-slim", arch=None,
                 workspace="/workspace", kernel=None, memory_mib=2048,
                 cpus=2, home=None, boot_timeout=30.0, packages=None,
                 host_objects=None, allow=None, cache=None, on_emit=None,
                 restore_from=None):
        rootfs = pathlib.Path(type(self).rootfs)
        kernel_path = pathlib.Path(type(self).kernel_image)
        if restore_from is not None:
            # Mirrors VmSession: refuse a snapshot booted from other
            # bits BEFORE anything is spent, so a stale one costs no
            # boot — which is why this runs ahead of super().__init__,
            # where the boot counter lives.
            goldenmod.verify(restore_from, rootfs, kernel_path)
        super().__init__(image=image, arch=arch, workspace=workspace,
                         kernel=kernel, memory_mib=memory_mib, cpus=cpus,
                         home=home, boot_timeout=boot_timeout,
                         packages=packages, host_objects=host_objects,
                         allow=allow, cache=cache, on_emit=on_emit)
        self.build = types.SimpleNamespace(rootfs_path=rootfs)
        self._kernel_path = kernel_path
        self.frozen = False
        self.freezes = 0
        self.thaws = 0
        self.thaw_fails = False
        # A real session's snapshot lands in its rundir; the pool copies
        # from there when it seeds a golden.
        self.restored_from = restore_from
        self._rundir = tempfile.mkdtemp(prefix="fakevm-")
        for name in ("vmstate", "mem"):
            pathlib.Path(self._rundir, name).write_bytes(b"snapshot")

    def freeze(self):
        self.frozen = True
        self.freezes += 1

    def thaw(self):
        if self.thaw_fails:
            raise ConnectionError("snapshot corrupt")
        self.frozen = False
        self.thaws += 1


def _fc_pool(monkeypatch, **kw):
    FakeVM.booted = 0  # the counter lives on the base class
    # Same reason as _pool: a background seed boots a second machine and
    # races every `booted ==` assertion. This helper was missing it, and
    # the race resolved the harmless way on a dev laptop and the other
    # way on CI. Tests about seeding pass auto_seed=True.
    kw.setdefault("auto_seed", False)
    kw.setdefault("session_cls", FrozenFakeVM)
    return poolmod.VmPool(**kw)


def test_an_affinity_park_is_kept_hot_not_frozen(monkeypatch, tmp_path):
    """The one thing a clone cannot reproduce is a WORKSPACE, so a
    tagged park is kept — but kept running. Freezing it would cost ~3s
    of every release that took this path, to save RAM on a VM you are
    parking precisely because you expect to come back to it."""
    _golden_home(monkeypatch, tmp_path)
    p = _fc_pool(monkeypatch, max_affinity=1)
    s = p.acquire()
    s.park_state = "commit-abc"
    s.close()
    assert not s.frozen and s.freezes == 0, "froze an affinity park"
    assert not s.torn_down
    assert s.requests == ["reset_guest"]  # hygiene still runs

    booted = FakeVM.booted
    s2 = p.acquire(state="commit-abc")
    assert s2 is s and s2.resumed and s2.thaws == 0
    assert FakeVM.booted == booted  # reuse, not a boot


def test_a_plain_release_keeps_nothing(monkeypatch, tmp_path):
    """Parking an untagged VM on a cloning rung buys nothing a 40ms
    clone does not, and used to cost a ~3s freeze to do it."""
    _golden_home(monkeypatch, tmp_path)
    p = _fc_pool(monkeypatch)
    s = p.acquire()
    s.close()
    assert s.freezes == 0 and s.torn_down


def test_frozen_idles_are_invisible_to_max_total(monkeypatch):
    """A frozen park is files, not RAM: booting past the cap must not
    sacrifice it, and it must not count against the cap."""
    p = _fc_pool(monkeypatch, max_total=1, max_affinity=1)
    a = p.acquire(image="a")
    a.park_state = "commit-a"
    a.close()      # hot affinity park
    a.freeze()     # frozen by hand: releases never freeze now
    b = p.acquire(image="b")  # boots; cap=1 must NOT reclaim the frozen park
    assert not a.torn_down
    assert FakeVM.booted == 2
    c = p.acquire(image="a", state="commit-a")  # thaw the park, no boot
    assert c is a and c.thaws == 1
    b.close()
    c.close()


def test_failed_thaw_falls_back_to_fresh_boot(monkeypatch):
    p = _fc_pool(monkeypatch)
    s = p.acquire()
    s.close()
    s.thaw_fails = True
    s2 = p.acquire()
    assert s2 is not s and s.torn_down
    assert FakeVM.booted == 2


def test_prewarm_parks_frozen(monkeypatch):
    p = _fc_pool(monkeypatch)
    p.prewarm(1, background=False, image="warm")
    bucket = p._idle[poolmod._fingerprint({"image": "warm"}, FrozenFakeVM)]
    assert len(bucket) == 1 and bucket[0][2].frozen


def test_vfkit_pool_never_freezes(monkeypatch):
    """Hot posture unchanged: no freeze attr, park keeps the VM live."""
    p = _pool(monkeypatch)
    s = p.acquire()
    s.close()
    assert not hasattr(s, "frozen") and not s.torn_down
    s2 = p.acquire()
    assert s2 is s


def test_make_room_never_victimizes_frozen_parks(monkeypatch):
    """Under cap pressure the victim scan must skip frozen idles
    (reclaiming files frees no RAM) and fall through to the quiet
    bound LRU. Order matters: the frozen park must already be idle
    when the scan runs at-cap, or the early total check hides it."""
    p = _fc_pool(monkeypatch, max_total=1, max_affinity=1)
    parked = p.acquire(image="parked")
    parked.park_state = "commit-p"
    parked.close()                     # hot affinity park
    parked.freeze()                    # frozen by hand: total 0
    held = p.acquire(image="held")     # bound, running: total = 1
    fresh = p.acquire(image="fresh")   # at cap -> scan runs, skips the
    assert held.torn_down              # frozen park, reclaims the LRU
    assert not parked.torn_down and parked.frozen
    fresh.close()


def test_refill_cap_ignores_frozen_parks(monkeypatch):
    """prewarm targets fill past max_total when the parks freeze —
    frozen warmth is disk, not RAM, so the cap doesn't apply."""
    p = _fc_pool(monkeypatch, max_total=2)
    p.prewarm(3, background=False, image="warm")
    bucket = p._idle[poolmod._fingerprint({"image": "warm"}, FrozenFakeVM)]
    assert len(bucket) == 3 and all(s.frozen for _, _, s in bucket)


# ---- releasing a reader blocked on a reclaimed channel ------------------
#
# _make_room reclaims a bound session whose _in_flight is 0 and accepts
# racing the owner's next call. If that race tears a frame, the owner's
# recv loop can be waiting out a bogus length prefix — a hang, so
# invisible to every `except` on the path. The deadline bounds it in
# general; aborting the socket ends it at once.


class _RecordingSock:
    def __init__(self):
        self.shutdowns = 0

    def shutdown(self, how):
        self.shutdowns += 1


def test_abort_is_scoped_to_sessions_that_had_an_owner(monkeypatch):
    """Idle victims (TTL, overflow, a park that failed to come back)
    have no second thread, so they keep their graceful poweroff. Only a
    reclaim-from-an-owner can have a reader to release."""
    p = _pool(monkeypatch, max_idle=4)
    owned = p.acquire(image="x")
    owned_sock = _RecordingSock()
    owned._ch._sock = owned_sock

    idle = p.acquire(image="y")
    idle_sock = _RecordingSock()
    idle._ch._sock = idle_sock
    idle.close()  # parks it: no longer bound

    p._teardown(owned)
    p._teardown(idle)

    assert owned_sock.shutdowns == 1
    assert idle_sock.shutdowns == 0


def test_abort_skips_a_frozen_park(monkeypatch):
    """A frozen park is files, not a process: there is no live channel
    and no reader to release."""
    p = _pool(monkeypatch)
    s = p.acquire(image="x")
    sock = _RecordingSock()
    s._ch._sock = sock
    s.frozen = True
    p._teardown(s)
    assert sock.shutdowns == 0


def test_abort_tolerates_a_channel_with_no_socket(monkeypatch):
    """Teardown must never fail on cleanup — a half-built session (boot
    raised before the channel existed) still has to be disposable."""
    p = _pool(monkeypatch)
    s = p.acquire(image="x")
    p._teardown(s)  # FakeVM's channel has no _sock at all
    assert s.torn_down


def test_teardown_releases_a_reader_blocked_on_a_bogus_length():
    """The end of the race PR #20 could only translate the errors of.

    A live thread here rather than injected frames: the point is that
    the reader is *blocked with no exception pending*, which is a state
    only a real blocking read can be in.
    """
    import socket as socketlib
    import struct
    import threading
    import time

    from dud.backends.base import HostSession, SessionLost
    from dud.proto import Channel

    class _SockSession(HostSession):
        def __init__(self, sock):
            super().__init__()
            self._ch = Channel(sock)
            self._pool = None
            self._pool_kwargs = {}
            self._scratch_master = None
            self.closed = False

        def close(self):
            self.closed = True

    a, b = socketlib.socketpair()
    try:
        s = _SockSession(a)
        p = poolmod.VmPool(session_cls=FakeVM)
        p._bound[id(s)] = s

        caught: list[BaseException] = []

        def owner():
            try:
                s.ping()
            except BaseException as e:  # noqa: BLE001 — the test IS the assert
                caught.append(e)

        t = threading.Thread(target=owner, daemon=True)
        t.start()
        # Commit the reader to a read that will never complete.
        b.sendall(struct.pack(">I", 0x0FFFFFFF) + b"partial")
        time.sleep(0.2)
        assert t.is_alive(), "reader should be blocked, not finished"

        started = time.monotonic()
        p._teardown(s)
        t.join(timeout=5.0)
        assert not t.is_alive(), "teardown did not release the reader"
        # Fast: proving the abort did it, not the verb's 30s deadline.
        assert time.monotonic() - started < 5.0
        assert caught and isinstance(caught[0], SessionLost)
    finally:
        a.close()
        b.close()


def test_reclaiming_a_bound_vm_aborts_its_channel(monkeypatch):
    """The production reclaim path, which the unit test above skips.

    _make_room has to drop its victim from _bound before calling
    _teardown — its capacity loop counts _bound, so leaving the victim
    there would never fall below the cap and it would pick the same one
    forever. Teardown therefore cannot rediscover that the session had
    an owner; it has to be told, and when it wasn't, the abort was dead
    code on exactly the path it exists for.
    """
    p = _no_auto(_pool(monkeypatch, max_total=1))
    a = p.acquire(image="x")
    sock = _RecordingSock()
    a._ch._sock = sock
    p.acquire(image="y")  # no idle to take: reclaims `a` from its owner
    assert a.torn_down is True
    assert sock.shutdowns == 1


def test_a_lost_bound_session_is_reaped_on_the_next_acquire(monkeypatch):
    """The recovery path's most likely mistake, made harmless.

    Rebinding `s` to a fresh session frees nothing — the pool holds a
    reference too — so a wedged VM would keep its memory until the
    process exited. A lost session can never be used again, so
    reclaiming it costs its owner nothing.
    """
    p = _pool(monkeypatch)
    dead = p.acquire(image="x")
    dead.dead = True
    dead._lost = "guest lost during 'exec_python'"
    # No close(): exactly what "s = dud.session(...)" alone would leave.

    p.acquire(image="x")

    assert id(dead) not in p._bound
    assert dead.torn_down


def test_a_live_bound_session_survives_the_sweep(monkeypatch):
    """The half that makes the sweep safe rather than merely effective.

    A reclaim that took healthy sessions would be the `max_total` path
    without its justification — that one interrupts a live owner
    deliberately and logs it as a loss. This one must only ever collect
    sessions that were already unusable.
    """
    p = _pool(monkeypatch)
    live = p.acquire(image="x")

    p.acquire(image="x")

    assert id(live) in p._bound
    assert not live.torn_down
    assert live._request("ping") == ({}, [])  # still usable


def test_pool_close_tears_down_bound_sessions_too(monkeypatch):
    """Their close() routes through release(), which would park them in
    the _idle of a pool no longer serving anyone — so if close() does
    not take them, nothing does."""
    p = _pool(monkeypatch)
    bound = p.acquire(image="x")
    p.close()
    assert p._bound == {}
    assert bound.torn_down


# ---- golden snapshots on the miss path ---------------------------------


def _golden_home(monkeypatch, tmp_path):
    """Point the golden store at a temp dir, not the real dud home."""
    monkeypatch.setattr(goldenmod, "dud_home", lambda: tmp_path)
    return tmp_path


def test_a_miss_seeds_the_template_in_the_background(monkeypatch, tmp_path):
    """Seeded off the critical path entirely: the caller who misses
    gets their boot and waits for nothing, and the template is built on
    its own machine. Seeding from their session, or from its release,
    would put a ~3s freeze on somebody's path to save a later 40ms
    clone — the trade that made pooling slower than not pooling."""
    _golden_home(monkeypatch, tmp_path)
    p = _pool(monkeypatch, session_cls=FrozenFakeVM, max_idle=0)
    key = poolmod._fingerprint({"image": "x"}, FrozenFakeVM)

    p.auto_seed = True
    real = p.seed  # run the background seed inline so the test can see it
    monkeypatch.setattr(p, "seed", lambda **kw: real(background=False, **kw))
    s = p.acquire(image="x")
    s.close()
    assert s.freezes == 0, "the caller's own session was frozen"
    assert goldenmod.usable(goldenmod.golden_dir(key))


def test_a_miss_clones_the_golden_instead_of_booting(monkeypatch, tmp_path):
    _golden_home(monkeypatch, tmp_path)
    p = _pool(monkeypatch, session_cls=FrozenFakeVM, max_idle=0)
    p.seed(background=False, image="x")
    booted = FrozenFakeVM.booted

    s = p.acquire(image="x")
    assert s.restored_from is not None, "miss cold-booted despite a golden"
    assert FrozenFakeVM.booted == booted + 1
    s.close()
    p.close()


def test_an_affinity_park_does_not_become_the_template(monkeypatch, tmp_path):
    """A park with `state` keeps one session's workspace on the VM.
    That is the opposite of what a template should carry — a clone of
    it would hand every later session somebody else's tree."""
    _golden_home(monkeypatch, tmp_path)
    p = _pool(monkeypatch, session_cls=FrozenFakeVM)
    s = p.acquire(image="x")
    key = poolmod._fingerprint(s._pool_kwargs, FrozenFakeVM)
    s.park_state = "commit-abc"  # what close(park_state=...) stamps
    s.close()
    assert not goldenmod.usable(goldenmod.golden_dir(key))
    p.close()


def test_an_unrestorable_golden_falls_back_to_booting(monkeypatch, tmp_path):
    """A golden snapshot is a cache. One that will not restore must
    cost speed, never a session — and must not be tried again."""
    _golden_home(monkeypatch, tmp_path)

    class Refuses(FrozenFakeVM):
        def __init__(self, **kw):
            if kw.get("restore_from") is not None:
                raise RuntimeError("snapshot from an older firecracker")
            super().__init__(**kw)

    p = _pool(monkeypatch, session_cls=Refuses, max_idle=0)
    p.seed(background=False, image="x")
    key = poolmod._fingerprint({"image": "x"}, Refuses)
    assert goldenmod.usable(goldenmod.golden_dir(key))

    s = p.acquire(image="x")           # must not raise
    assert s.restored_from is None     # booted instead
    assert not goldenmod.usable(goldenmod.golden_dir(key))  # and dropped
    s.close()
    p.close()


def test_a_rung_that_cannot_snapshot_is_untouched(monkeypatch, tmp_path):
    """vfkit has no freeze/thaw at all; it must keep cold-booting on a
    miss rather than reaching for a store it can never fill."""
    _golden_home(monkeypatch, tmp_path)
    p = _pool(monkeypatch)  # plain FakeVM: no freeze
    s = p.acquire(image="x")
    assert not hasattr(s, "restored_from") or s.restored_from is None
    s.close()
    assert not (tmp_path / "golden").exists()
    p.close()


def test_seed_builds_the_template_before_anyone_asks(monkeypatch, tmp_path):
    """Pre-warming without the cost of staying warm. On the frozen
    posture a template is a file — no VM, no RAM — and any number of
    sessions can start from it, so there is no warm level to pick."""
    _golden_home(monkeypatch, tmp_path)
    p = _pool(monkeypatch, session_cls=FrozenFakeVM, max_idle=0)
    key = poolmod._fingerprint({"image": "x"}, FrozenFakeVM)

    p.seed(background=False, image="x")
    assert goldenmod.usable(goldenmod.golden_dir(key))
    assert FrozenFakeVM.booted == 1  # exactly one boot, and it is gone

    s = p.acquire(image="x")
    assert s.restored_from is not None, "first session still cold-booted"
    s.close()
    p.close()


def test_seed_is_idempotent(monkeypatch, tmp_path):
    _golden_home(monkeypatch, tmp_path)
    p = _pool(monkeypatch, session_cls=FrozenFakeVM)
    p.seed(background=False, image="x")
    booted = FrozenFakeVM.booted
    p.seed(background=False, image="x")
    assert FrozenFakeVM.booted == booted  # no second boot for a template we have
    p.close()


def test_seed_does_nothing_on_a_rung_without_snapshots(monkeypatch, tmp_path):
    _golden_home(monkeypatch, tmp_path)
    p = _pool(monkeypatch)  # plain FakeVM: no freeze
    p.seed(background=False, image="x")
    assert FrozenFakeVM.booted == 0 or not (tmp_path / "golden").exists()
    p.close()


def test_seed_survives_an_environment_that_cannot_boot(monkeypatch, tmp_path):
    """Requesting an optimisation must never raise: no kernel, no KVM,
    no vfkit are all ordinary reasons a template cannot be built."""
    _golden_home(monkeypatch, tmp_path)

    class CannotBoot(FrozenFakeVM):
        def __init__(self, **kw):
            raise OSError("no /dev/kvm here")

    p = _pool(monkeypatch, session_cls=CannotBoot)
    p.seed(background=False, image="x")  # must not raise
    assert not (tmp_path / "golden").exists()
    p.close()


def test_prewarm_also_leaves_a_template(monkeypatch, tmp_path):
    """prewarm freezes and parks directly rather than going through
    release, so it used to warm the pool and still leave the first miss
    past the warm level cold-booting."""
    _golden_home(monkeypatch, tmp_path)
    p = _pool(monkeypatch, session_cls=FrozenFakeVM)
    key = poolmod._fingerprint({"image": "x"}, FrozenFakeVM)
    p.prewarm(1, background=False, image="x")
    assert goldenmod.usable(goldenmod.golden_dir(key))
    p.close()


def test_a_scratch_config_neither_seeds_nor_clones(monkeypatch, tmp_path):
    """Not a missed optimisation — a loop.

    A snapshot records the seed's per-boot `<rundir>/scratch.img`, and
    seeding then deletes that rundir. Every later restore would
    reference a file that is gone, and because a failed restore
    discards the snapshot and reseeds, each miss would pay a failed
    restore, a cold boot AND a background boot-plus-freeze, forever.
    """
    _golden_home(monkeypatch, tmp_path)

    class ScratchFake(FrozenFakeVM):
        def __init__(self, scratch=None, **kw):
            self.scratch = scratch
            super().__init__(**kw)

    p = _fc_pool(monkeypatch, session_cls=ScratchFake, auto_seed=True,
                 max_idle=0)
    real = p.seed
    monkeypatch.setattr(p, "seed", lambda **kw: real(background=False, **kw))

    s = p.acquire(image="x", scratch="/vol/cache.img")
    s.close()
    assert not (tmp_path / "golden").exists(), "seeded a scratch config"
    assert FrozenFakeVM.booted == 1, "a scratch miss booted more than once"
    p.close()


def test_a_background_seed_answers_to_max_total(monkeypatch, tmp_path):
    """A seed is a whole VM, and used to be built outside both _bound
    and _idle — invisible to the cap and to _make_room. A sequential
    acquire on max_total=1 could therefore run two full guests."""
    _golden_home(monkeypatch, tmp_path)
    p = _fc_pool(monkeypatch, auto_seed=True, max_total=1, max_idle=0)
    real = p.seed
    monkeypatch.setattr(p, "seed", lambda **kw: real(background=False, **kw))

    s = p.acquire(image="x")  # the caller's own VM fills the cap
    assert FrozenFakeVM.booted == 1, "a seed booted past max_total=1"
    assert not (tmp_path / "golden").exists()
    s.close()
    p.close()


def test_a_seed_reserves_its_slot_before_it_boots(monkeypatch, tmp_path):
    """Counting the reservation rather than the constructed VM is the
    point: two seeds must not both look at an empty pool and both
    boot."""
    _golden_home(monkeypatch, tmp_path)
    p = _fc_pool(monkeypatch, auto_seed=False, max_total=1)
    with p._lock:
        p._seeding.add("some-other-key")
        assert p._at_capacity_locked()
        assert p._live_locked() == 1


def test_affinity_is_off_by_default(monkeypatch, tmp_path):
    """Measured, not assumed. An affinity park keeps a whole VM alive
    to skip one push_tree — and a push is ~40 us per file, so on the
    dozens-of-files trees a real agent workspace actually holds, that
    is 3-9 ms against a ~45 ms acquire. Not a 1-2 GiB trade to make on
    someone's behalf."""
    _golden_home(monkeypatch, tmp_path)
    p = _fc_pool(monkeypatch)
    assert p.max_affinity == 0
    s = p.acquire(image="x")
    s.close(park_state="commit-abc")
    assert s.torn_down, "a tagged release parked despite affinity being off"
    p.close()


def test_an_ignored_tag_says_so_once(monkeypatch, tmp_path, caplog):
    """The hazard that keeps this a default rather than a deletion:
    with affinity off, park_state is a request that quietly does
    nothing and `resumed=False` forever is not a readable symptom.

    Once per pool, though — it is a configuration mismatch, not a
    per-release event, and a warning on every close would be noise the
    user learns to skip."""
    import logging

    _golden_home(monkeypatch, tmp_path)
    p = _fc_pool(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="dud.backends.pool"):
        for _ in range(3):
            p.acquire(image="x").close(park_state="commit-abc")
    warnings = [r for r in caplog.records if "max_affinity" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "resumed=False" in warnings[0].getMessage()
    p.close()


def test_no_tag_no_warning(monkeypatch, tmp_path, caplog):
    """The overwhelmingly common path must stay silent: a caller who
    never asked for affinity is not misconfigured."""
    import logging

    _golden_home(monkeypatch, tmp_path)
    p = _fc_pool(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="dud.backends.pool"):
        p.acquire(image="x").close()
    assert not [r for r in caplog.records if "max_affinity" in r.getMessage()]
    p.close()
