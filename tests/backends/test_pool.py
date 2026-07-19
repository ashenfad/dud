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

    def ping(self):
        if self.dead:
            raise ConnectionError("vm died")
        return {"pong": True}

    def promote_scratch(self):
        # Mirrors the real guard: teardown disarms by clearing the master.
        if self._scratch_master is not None:
            self.promotions += 1

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
    b = p.acquire(image="y")  # over-boots rather than blocking
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

    def __init__(self, image="python:3.12-slim", arch=None,
                 workspace="/workspace", kernel=None, memory_mib=2048,
                 cpus=2, home=None, boot_timeout=30.0, packages=None,
                 host_objects=None, allow=None, cache=None, on_emit=None):
        super().__init__(image=image, arch=arch, workspace=workspace,
                         kernel=kernel, memory_mib=memory_mib, cpus=cpus,
                         home=home, boot_timeout=boot_timeout,
                         packages=packages, host_objects=host_objects,
                         allow=allow, cache=cache, on_emit=on_emit)
        self.frozen = False
        self.freezes = 0
        self.thaws = 0
        self.thaw_fails = False

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
    return poolmod.VmPool(session_cls=FrozenFakeVM, **kw)


def test_release_freezes_and_acquire_thaws(monkeypatch):
    p = _fc_pool(monkeypatch)
    s = p.acquire()
    s.close()
    assert s.frozen and s.freezes == 1
    assert s.requests == ["reset_guest"]  # hygiene BEFORE the freeze
    s2 = p.acquire()
    assert s2 is s and not s2.frozen and s2.thaws == 1
    assert FakeVM.booted == 1  # reuse, not a boot


def test_frozen_idles_are_invisible_to_max_total(monkeypatch):
    """A frozen park is files, not RAM: booting past the cap must not
    sacrifice it, and it must not count against the cap."""
    p = _fc_pool(monkeypatch, max_total=1)
    a = p.acquire(image="a")
    a.close()  # parked frozen under image=a
    b = p.acquire(image="b")  # boots; cap=1 must NOT reclaim the frozen park
    assert not a.torn_down
    assert FakeVM.booted == 2
    c = p.acquire(image="a")  # thaw the park, no boot
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
    p = _fc_pool(monkeypatch, max_total=1)
    parked = p.acquire(image="parked")
    parked.close()                     # frozen idle: invisible, total 0
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
