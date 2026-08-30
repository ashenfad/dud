"""Reuse VMs across sessions: same image, new state, no boot.

The design premise makes VMs fungible — files ride in via ``push_tree``,
cache and host objects live host-side, python state dies with each
runner — so a session's identity never touches the machine. A pool
keyed by the *boot fingerprint* (image, packages, kernel, sizing) hands
an idle VM to the next session for the cost of a ``reset_guest`` +
``push_tree`` (~100s of ms) instead of a boot (~seconds).

Two parking postures, chosen by what the backend can do:

- **hot** (vfkit): the parked VM keeps running; reuse is reset + push.
  Idle warmth costs RAM (macOS pages untouched guest memory out, so
  less than the headline size, but not nothing).
- **frozen** (firecracker): the parked VM is snapshotted to files and
  its VMM killed; reuse is a thaw (~tens of ms — the memory file is
  mmap'd, not read). Idle warmth costs disk only, so frozen VMs are
  invisible to ``max_total`` and never reclaimed for RAM pressure.

The posture is duck-typed off the session (``freeze``/``thaw``); the
acquire/release contract, fingerprints, affinity tags, and caps are
identical either way.

Hygiene on release, not acquire (secrets leave promptly): wipe both
trees, restore boot-time shell env, and kill every non-supervisor
process in the guest (see ``Supervisor.do_reset_guest``). Residue
*outside* the workspace (``/tmp``, absolute-path writes, warmed
``__pycache__``) survives reuse — acceptable within one user's studio,
and the warmed imports are a feature; overlay-at-root is the eventual
stricter reset (see ROADMAP).

Scope: in-process only, deliberately. A VM dies with this process —
channel EOF powers the guest off, vfkit exits with the guest — so a
studio crash can't strand VMs. That linkage is an invariant, not a
gap: state lives in kvgit and boots are ~1 s, so surviving restarts
would buy almost nothing and cost the cascade that makes cleanup free
(see ROADMAP "Deliberately not now").

Capacity: the pool is a cache, not a semaphore — ``acquire`` never
blocks. ``max_total`` adds demand-driven reclaim: before booting past
the cap, tear down the global-LRU *idle* VM, then the LRU *bound* VM
that isn't mid-request. A reclaimed owner's next call raises
:class:`~dud.backends.base.SessionLost` and its recovery path
(re-acquire + push from the provider) revives it — the disposable
thesis as a capacity policy.
"""

from __future__ import annotations

import atexit
import inspect
import json
import logging
import os
import socket as socketlib
import threading
import time
from typing import Any

from ..errors import DudError
from .base import require_allowlist
from . import golden
from .vm import VmSession

# Everything the pool does on the failure side is recoverable, which is
# exactly why it is worth reporting: silent recovery is indistinguishable
# from "boots are mysteriously slow". DEBUG carries routine bookkeeping
# (hits, misses, TTL), INFO the recoveries a fresh boot papers over, and
# WARNING the two losses somebody actually pays for — a reclaimed VM
# that still has an owner, and warmth that could not be parked.
_log = logging.getLogger(__name__)

# Host-side binding kwargs: per-session state rebound on reuse, never
# part of the VM's identity.
_BINDING_KEYS = ("host_objects", "allow", "cache", "on_emit",
                 "outputs_hook", "render_hook")
# Constructor kwargs that don't change what was booted.
_NON_IDENTITY = ("boot_timeout",)


def _fingerprint(kwargs: dict[str, Any], session_cls: type = None) -> str:
    """Boot-identity hash, normalized against the constructor's defaults
    so sparse call-site kwargs and a session's fully-captured
    ``_pool_kwargs`` produce the SAME key (acquire must find what release
    parked).

    ``medium`` compares RAW, pre-resolution: ``"auto"`` and an explicit
    ``"initramfs"`` are different keys even when auto resolves to
    initramfs (resolution needs image inspection this hash must not
    do). Self-consistent either way — just pick one style per app, or
    mixed call sites warm separate buckets."""
    params = inspect.signature(
        (session_cls or _default_cls()).__init__
    ).parameters
    ident: dict[str, Any] = {}
    for name, p in params.items():
        if name == "self" or name in _BINDING_KEYS or name in _NON_IDENTITY:
            continue
        default = None if p.default is inspect.Parameter.empty else p.default
        ident[name] = kwargs.get(name, default)
    return json.dumps(ident, sort_keys=True, default=str)


def _default_cls() -> type:
    """The VM rung this host can run.

    The pool itself is rung-agnostic — it only needs *a* class to
    construct and to read constructor defaults off. Defaulting to a
    named backend (it used to be vfkit) quietly made rung 2 the norm
    and rung 3 the special case, which is backwards on a Linux fleet.
    """
    import platform

    if platform.system() == "Darwin":
        from .vfkit import VfkitSession

        return VfkitSession
    from .firecracker import FirecrackerSession

    return FirecrackerSession


class VmPool:
    """Idle VMs keyed by boot fingerprint.

    ``acquire`` returns a session whose ``close()`` parks the VM here
    (after guest reset) instead of powering it off; the pool tears VMs
    down on idle-cap overflow, TTL expiry (checked lazily), ``close()``,
    or process exit. ``session_cls`` picks the rung (default vfkit);
    sessions that can ``freeze`` park frozen (see the module docstring).
    """

    def __init__(
        self,
        max_idle: int = 2,
        ttl: float = 900.0,
        max_total: int | None = None,
        session_cls: type | None = None,
        max_affinity: int = 1,
        auto_seed: bool = True,
    ):
        # How many tagged VMs to hold RUNNING **per boot fingerprint**
        # for a same-content resume, on a rung that can clone. Note the
        # per-key part: a pool serving several configs holds up to this
        # many for each, and `max_total` (None by default) is the only
        # global bound.
        #
        # An affinity park costs real RAM and buys exactly one thing: a
        # skipped push_tree, since a plain miss now restores a clone in
        # ~40 ms rather than booting.
        #
        # Measured (dev/pushbench.py, vfkit/arm64): a push runs ~40 us
        # per file, linearly, out to 20k files — 26 ms for a 500-file
        # scratch tree, 377 ms for a 10k-file workspace with its
        # dependencies installed. So a hit saves tens of milliseconds
        # on small trees and most of a second on real ones.
        #
        # 1 rather than 0 mostly because 0 would make `park_state` a
        # silent no-op: a caller who explicitly asks for affinity would
        # get `resumed=False` forever with nothing to explain why. The
        # cost is opt-in either way — `release` only parks a VM whose
        # owner stamped a tag, so a caller who never tags never holds
        # one. 0 disables it for consumers who would rather have the
        # RAM.
        self.max_affinity = max_affinity
        # Build a template automatically the first time a config misses.
        # A consumer that wants templates only where it says so — or
        # that cannot spare the one extra boot — turns this off and
        # calls seed() itself.
        self.auto_seed = auto_seed
        self.max_idle = max_idle
        self.ttl = ttl
        self.max_total = max_total
        self.session_cls = session_cls or _default_cls()
        self._idle: dict[str, list[tuple[float, VmSession]]] = {}
        # Bound = checked out and held by a session owner. Tracked so
        # max_total can reclaim the LRU one under demand (id() keys:
        # sessions aren't hashable-by-value and identity is the point).
        self._bound: dict[int, VmSession] = {}
        self._targets: dict[str, tuple[int, dict[str, Any]]] = {}
        self._filling: set[str] = set()
        # Fingerprints whose template is being built right now, so a
        # burst of misses triggers one background boot rather than one
        # per miss.
        self._seeding: set[str] = set()
        self._lock = threading.Lock()
        atexit.register(self.close)

    # ---- lifecycle ----------------------------------------------------

    def acquire(self, state: str | None = None, **kwargs: Any) -> VmSession:
        """Hand out a VM for this config; prefer one parked with tag
        ``state`` (content-addressed workspace identity, e.g. a kvgit
        commit). On a tag match the returned session has
        ``resumed=True`` — its tree already IS that state, so the caller
        skips the push and just continues. Any other VM (or a fresh
        boot) comes back ``resumed=False``."""
        # Before anything expensive: a bad allowlist must not cost a VM.
        # _make_room() below can reclaim somebody else's session to fit a
        # boot that is about to raise in the constructor anyway.
        require_allowlist(kwargs.get("host_objects"), kwargs.get("allow"))
        key = _fingerprint(kwargs, self.session_cls)
        binding = {k: kwargs.get(k) for k in _BINDING_KEYS}
        while True:
            matched = False
            with self._lock:
                stale = self._expire_locked()
                # Swept here rather than on a timer: acquire is when the
                # pool is about to spend resources, so it is the moment
                # worth reclaiming any that are provably wasted.
                stale += self._reap_lost_locked()
                bucket = self._idle.get(key) or []
                parked = None
                if bucket:
                    if state is not None:
                        for i, (_, tag, _s) in enumerate(bucket):
                            if tag == state:
                                parked = bucket.pop(i)
                                matched = True
                                break
                    if parked is None:
                        # MRU: the newest parked VM has the hottest page
                        # cache and warmest imports; the oldest idles
                        # toward TTL/reclaim, which is how excess warmth
                        # should shed.
                        parked = bucket.pop(0)
            if stale:
                _log.debug("TTL expired %d idle VM(s)", len(stale))
            for s in stale:
                self._teardown(s)
            if parked is None:
                self._make_room()
                self._maybe_refill(key)  # replace what we're about to boot
                session, cold = self._fresh(key, kwargs)
                session._pool = self  # close() -> release
                session.resumed = False
                with self._lock:
                    self._bound[id(session)] = session
                if cold and self.auto_seed:
                    # Seeded only after this session is bound, so the
                    # caller's own VM is counted when the seed asks
                    # whether max_total has room for one more. Seeding
                    # from inside _fresh looked at a pool that did not
                    # yet contain the very VM being booted.
                    self.seed(**kwargs)
                return session
            _, _, session = parked
            try:
                # Frozen parks resume here (thaw = new VMM over the
                # snapshot files); hot parks just prove liveness. A
                # thaw materializes a running VM the cap never counted
                # (frozen = files), so make room first — same pressure
                # valve as a fresh boot, and just as non-blocking.
                if getattr(session, "frozen", False):
                    self._make_room()
                    session.thaw()
                else:
                    session.ping()
            except Exception:  # noqa: BLE001 — any failure means "not usable"
                _log.info("parked VM did not come back; booting fresh",
                          exc_info=True)
                self._teardown(session)
                continue  # dead while parked: boot fresh next loop
            _log.debug("pool hit (resumed=%s)", matched)
            self._maybe_refill(key)  # top the level back up in background
            self._rebind(session, binding)
            session.resumed = matched
            with self._lock:
                self._bound[id(session)] = session
            return session

    def _seed_golden(self, key: str, frozen_session: Any) -> None:
        """Publish an already-frozen session as this config's template.

        Always best-effort: a golden snapshot is a cache, and failing to
        make one costs a boot rather than a session.
        """
        path = golden.golden_dir(key)
        if golden.usable(path):
            return
        try:
            golden.publish_frozen(frozen_session, path)
        except Exception:  # noqa: BLE001 — a cache miss, not an error
            _log.debug("could not seed a golden snapshot", exc_info=True)

    def seed(self, background: bool = True, **kwargs: Any) -> None:
        """Build this config's golden snapshot now, before anyone asks.

        The point of pre-warming without the cost of staying warm. On
        the frozen posture a template is a *file*: it holds no VM, no
        RAM and no process, and any number of sessions can start from
        it at once — so unlike ``prewarm``, there is no warm level to
        pick and nothing to shed. One boot, once, and every later miss
        is a clone (32-52 ms against 1276 ms cold, measured on amd64).

        Does nothing on a rung that cannot snapshot, and nothing if a
        template already exists. Failures are logged, never raised:
        this is an optimisation being requested, not a session.
        """
        if not hasattr(self.session_cls, "freeze"):
            return
        if not golden.eligible(kwargs):
            return
        key = _fingerprint(kwargs, self.session_cls)
        if golden.usable(golden.golden_dir(key)):
            return
        with self._lock:
            if key in self._seeding:
                return   # already being built; one boot is enough
            # A seed is a whole VM, so it answers to max_total like any
            # other. It used to be built outside both _bound and _idle,
            # which made it invisible to the cap and to _make_room: a
            # sequential acquire on max_total=1 could run two full
            # guests, and a burst across fingerprints could run several
            # more. Counting _seeding reserves the slot from here,
            # before the VM exists, so two seeds cannot both pass.
            if self._at_capacity_locked():
                _log.debug("at max_total=%s; skipping the background seed "
                           "(misses cold-boot until there is room)",
                           self.max_total)
                return
            self._seeding.add(key)
        boot_kwargs = {k: v for k, v in kwargs.items() if k not in _BINDING_KEYS}
        if background:
            threading.Thread(target=self._seed, args=(key, boot_kwargs),
                             daemon=True).start()
        else:
            self._seed(key, boot_kwargs)

    def _seed(self, key: str, boot_kwargs: dict[str, Any]) -> None:
        session = None
        try:
            session = self.session_cls(**boot_kwargs)
            session.freeze()
            self._seed_golden(key, session)
        except (DudError, OSError) as e:
            # Same expected-environment set prewarm tolerates: no
            # kernel, no KVM, no vfkit. Seeding is optional.
            _log.info("could not seed a golden snapshot: %s", e)
        finally:
            if session is not None:
                try:
                    session.close()   # frozen: disposal removes the rundir
                except Exception:  # noqa: BLE001 — cleanup, already done
                    _log.debug("close failed after seeding", exc_info=True)
            with self._lock:
                self._seeding.discard(key)

    def _fresh(self, key: str, kwargs: dict[str, Any]):
        """A machine for a miss, and whether it wants a template built.

        Returns ``(session, cold)``: a golden clone if there is one,
        else a cold boot — which then leaves a golden behind for next
        time.

        The boot a miss used to pay is a pure function of the boot
        fingerprint, so it is worth paying once rather than per miss.
        Measured on amd64 CI: a clone reaches a serving VM in 32-52 ms
        against 1276 ms cold.

        Deliberately does NOT seed the snapshot itself. Freezing here
        would make the first caller pay ~3 s of freeze plus a copy so
        that later ones save a boot — a bad trade for anyone who runs
        one session and leaves. `release` seeds it instead, out of a
        freeze it was going to do anyway.

        Every failure here falls back to booting. A golden snapshot is
        a cache: not having one, or having one that will not restore,
        must cost speed and never a session.
        """
        if not hasattr(self.session_cls, "freeze"):
            return self.session_cls(**kwargs), False  # rung cannot snapshot
        if not golden.eligible(kwargs):
            # Nothing to clone from and nothing worth seeding: a
            # scratch-backed config would publish a snapshot that
            # references a per-boot file already deleted.
            return self.session_cls(**kwargs), False

        path = golden.golden_dir(key)
        if golden.usable(path):
            try:
                session = self.session_cls(restore_from=path, **kwargs)
                _log.debug("pool miss: cloned the golden snapshot")
                return session, False
            except Exception:  # noqa: BLE001 — a bad cache is not a bad session
                _log.warning("golden snapshot would not restore; discarding "
                             "it and booting fresh", exc_info=True)
                golden.discard(key)

        _log.debug("pool miss: booting a fresh %s (no golden snapshot yet)",
                   self.session_cls.__name__)
        # The caller gets a boot; `acquire` builds the template behind
        # them, on its own machine, once this session is bound. They
        # wait for nothing, and every miss from here on is a clone.
        # Deliberately not seeded from THIS session or from its
        # release: both would put a ~3s freeze on somebody's critical
        # path to save a later 40ms clone, which is the trade that made
        # pooling slower than not pooling.
        return self.session_cls(**kwargs), True

    def _live_locked(self) -> int:
        """VMs holding RAM right now, which is what ``max_total`` caps.

        Frozen parks are files, not processes: they consume no RAM or
        CPU, so the cap neither counts them nor reclaims them (TTL is
        their only expiry — disk GC).

        In-flight seeds DO count. Each is a full guest, and counting
        the reservation rather than the constructed object is the point:
        the slot is claimed before the VM exists, so two seeds cannot
        both look at an empty pool and both boot.
        """
        return (
            len(self._bound)
            + len(self._seeding)
            + sum(1 for b in self._idle.values()
                  for _, _, s in b if not getattr(s, "frozen", False))
        )

    def _at_capacity_locked(self) -> bool:
        return (self.max_total is not None
                and self._live_locked() >= self.max_total)

    def _make_room(self) -> None:
        """Demand-driven reclaim: called before booting a fresh VM when
        ``max_total`` is set. Victims in preference order: the
        global-LRU *idle* VM (nobody notices), then the LRU *bound* VM
        with no request in flight (its owner's next call raises
        ``SessionLost`` and recovers by re-acquiring + re-pushing —
        ~1 s, landing on whoever has been quiet longest). If every VM
        is mid-request we over-boot rather than block: the cap is a
        pressure valve, not a semaphore. The in-flight check races an
        owner's next call by design — the recovery path makes losing
        that race an inconvenience, not an error."""
        if self.max_total is None:
            return
        while True:
            victim: Any = None
            with self._lock:
                if not self._at_capacity_locked():
                    return
                oldest: tuple[float, str, int] | None = None
                for key, bucket in self._idle.items():
                    # Prewarm-floor VMs (the newest n of a targeted
                    # key) are exempt, as with TTL: reclaiming one just
                    # triggers a re-boot churn loop under pressure.
                    floor = self._targets.get(key, (0, None))[0]
                    for i, (t, _tag, s) in enumerate(bucket):
                        if i < floor or getattr(s, "frozen", False):
                            continue
                        if oldest is None or t < oldest[0]:
                            oldest = (t, key, i)
                if oldest is not None:
                    _, key, i = oldest
                    _, _, victim = self._idle[key].pop(i)
                    bound = False
                else:
                    quiet = [
                        s for s in self._bound.values()
                        if getattr(s, "_in_flight", 0) == 0
                    ]
                    if not quiet:
                        _log.debug(
                            "at max_total=%s with every VM mid-request; "
                            "over-booting rather than blocking", self.max_total,
                        )
                        return  # all mid-request: over-boot, don't block
                    victim = min(
                        quiet, key=lambda s: getattr(s, "last_used", 0.0)
                    )
                    self._bound.pop(id(victim), None)
                    bound = True
            if bound:
                # The one reclaim somebody feels: this VM has an owner,
                # and their next call raises SessionLost. Recoverable by
                # design, but never something to discover by inference.
                _log.warning(
                    "at max_total=%s: reclaiming a VM still held by a "
                    "session; its next call will raise SessionLost",
                    self.max_total,
                )
            else:
                _log.info("at max_total=%s: reclaiming an idle VM",
                          self.max_total)
            self._teardown(victim, had_owner=bound)

    def prewarm(self, n: int, background: bool = True, **kwargs: Any) -> None:
        """Keep ``n`` idle VMs warm for this config: boot-and-park the
        deficit now (in a background thread by default), and re-fill
        whenever an acquire drains below ``n``. Targeted VMs are exempt
        from TTL expiry — holding them warm is the entire point. Callers
        opting in accept the idle RAM cost."""
        key = _fingerprint(kwargs, self.session_cls)
        boot_kwargs = {k: v for k, v in kwargs.items() if k not in _BINDING_KEYS}
        with self._lock:
            self._targets[key] = (max(0, n), boot_kwargs)
        if background:
            self._maybe_refill(key)
        else:
            self._refill(key)

    def _maybe_refill(self, key: str) -> None:
        with self._lock:
            target = self._targets.get(key)
            if target is None or key in self._filling:
                return
            n, _ = target
            if len(self._idle.get(key) or ()) >= n:
                return
            self._filling.add(key)
        threading.Thread(
            target=self._refill, args=(key,), kwargs={"claimed": True},
            daemon=True,
        ).start()

    def _refill(self, key: str, claimed: bool = False) -> None:
        if not claimed:
            with self._lock:
                if key in self._filling:
                    return
                self._filling.add(key)
        try:
            while True:
                with self._lock:
                    target = self._targets.get(key)
                    if target is None:
                        return
                    n, boot_kwargs = target
                    if len(self._idle.get(key) or ()) >= n:
                        return
                    if self.max_total is not None:
                        # Same arithmetic as _make_room: frozen parks
                        # are files, not RAM — invisible to the cap.
                        total = len(self._bound) + sum(
                            1
                            for b in self._idle.values()
                            for _, _, s in b
                            if not getattr(s, "frozen", False)
                        )
                        if total >= self.max_total:
                            return  # the cap outranks the warm target
                try:
                    session = self.session_cls(**boot_kwargs)
                except (DudError, OSError) as e:
                    # The environment can't prewarm (no kernel, no KVM,
                    # no vfkit): expected, and prewarming is optional.
                    # Deliberately NOT blind — a TypeError from bad
                    # boot kwargs is a bug, and letting it out of this
                    # daemon thread puts a traceback on stderr instead
                    # of silently leaving the pool permanently cold.
                    _log.info("prewarm unavailable, pool stays cold: %s", e)
                    return
                # Zero-RAM prewarm where the backend can: a frozen
                # freshly-booted VM is warmth as a file.
                try:
                    if hasattr(session, "freeze"):
                        session.freeze()
                        # Free: this VM is frozen either way, so the
                        # template costs a copy. Without it a prewarmed
                        # pool still cold-booted on the first miss past
                        # its warm level.
                        self._seed_golden(key, session)
                except Exception:  # noqa: BLE001 — unfreezable: discard it
                    _log.warning("prewarmed VM could not freeze; discarding",
                                 exc_info=True)
                    try:
                        session.close()
                    except Exception:  # noqa: BLE001 — cleanup, already failing
                        _log.debug("close failed on an unfreezable prewarm",
                                   exc_info=True)
                    return
                session._pool = self
                with self._lock:
                    self._idle.setdefault(key, []).insert(
                        0, (time.monotonic(), None, session)
                    )
        finally:
            with self._lock:
                self._filling.discard(key)

    def release(self, session: VmSession) -> None:
        """Reset the guest and park; a VM that fails reset is torn down.

        If the releasing owner stamped ``session.park_state`` (the
        content hash its tree corresponds to — dud never computes this,
        the layer above owns state identity), the tree is kept in place
        and parked under that tag for a same-state resume. Env/process
        hygiene runs either way; a mismatched later consumer is safe
        because push_tree wipes before extracting."""
        state = getattr(session, "park_state", None)
        session.park_state = None  # tags never survive a park cycle
        with self._lock:
            self._bound.pop(id(session), None)
        try:
            session._request("reset_guest", {"keep_tree": bool(state)})
        except Exception:  # noqa: BLE001 — an unresettable guest is not reusable
            _log.info("guest reset failed on release; discarding instead of "
                      "parking", exc_info=True)
            self._teardown(session)
            return
        # A successful reset means the guest is alive and synced: an
        # intermediate scratch promotion here means the cache survives
        # even if the parked VM later dies. Best-effort by the scratch
        # contract — a failed promotion is a cold cache, not an error.
        try:
            session.promote_scratch()
        except Exception:  # noqa: BLE001 — scratch is cache; a miss is not an error
            _log.debug("scratch promotion failed; next boot starts cold",
                       exc_info=True)
        key = _fingerprint(session._pool_kwargs, self.session_cls)
        if hasattr(session, "freeze"):
            # A rung that can clone never freezes on release, and never
            # parks an untagged VM. Parking here would mean snapshotting
            # (~3s for a 1 GiB guest) to save a ~40ms clone — which made
            # pooling ~2.4x SLOWER than not pooling at all.
            #
            # An affinity park is the one thing a clone cannot
            # reproduce: it holds a WORKSPACE. So it is kept, but kept
            # HOT — running costs RAM, which `max_affinity` bounds,
            # where freezing would cost three seconds of every release
            # that took this path.
            if state is None or self.max_affinity <= 0:
                self._teardown(session)
                return
        with self._lock:
            stale = self._expire_locked()
            bucket = self._idle.setdefault(key, [])
            bucket.insert(0, (time.monotonic(), state, session))
            limit = max(self.max_idle, self._targets.get(key, (0, None))[0])
            if hasattr(session, "freeze"):
                # Hot parks on a cloning rung: RAM, so bounded tightly.
                limit = self.max_affinity
            overflow = bucket[limit:]
            del bucket[limit:]
        for _, _, s in overflow:
            self._teardown(s)
        for s in stale:
            self._teardown(s)

    def close(self) -> None:
        """Tear down everything this pool holds, idle and bound alike.

        Bound sessions are included because nothing else will free
        them once the pool is gone: their ``close()`` routes through
        ``release()``, which would park them in the ``_idle`` of a pool
        that is no longer serving anyone. Leaving them was a leak with
        an alibi — the process-exit cascade (channel EOF powers the
        guest off, the VMM exits with it) meant no VM outlived the
        run, so the rundirs and the bookkeeping were the only visible
        cost, and only until the next boot's stale sweep.
        """
        with self._lock:
            buckets, self._idle = self._idle, {}
            bound, self._bound = list(self._bound.values()), {}
        for bucket in buckets.values():
            for _, _, s in bucket:
                self._teardown(s)
        for s in bound:
            # had_owner: somebody may still be holding this one and
            # blocked on it, and the pool is going away underneath them.
            self._teardown(s, had_owner=True)

    # ---- internals ----------------------------------------------------

    def _rebind(self, session: VmSession, binding: dict[str, Any]) -> None:
        # Checked again at the assignment point, not just in acquire:
        # these fields bypass the constructor entirely, and a security
        # default that holds on a fresh boot but lapses on a pool hit is
        # the worst possible shape for one.
        allow = require_allowlist(binding["host_objects"], binding["allow"])
        session.cache = binding["cache"] if binding["cache"] is not None else {}
        session.host_objects = binding["host_objects"] or {}
        session.allow = allow
        session.on_emit = binding["on_emit"]
        session.outputs_hook = binding["outputs_hook"]
        session.render_hook = binding["render_hook"]
        session.emits = []
        session._closed = False
        # Cleared for the same reason as `_closed`: these fields bypass
        # the constructor, and a latch left set would make a perfectly
        # live VM refuse its new owner. A session that was actually lost
        # cannot arrive here — release() discards one whose reset failed,
        # and acquire() tears down a park that will not ping or thaw —
        # so this is the defensive half of that, not the load-bearing one.
        session._lost = None

    @staticmethod
    def _abort_channel(session: VmSession) -> None:
        """Release anyone blocked reading this session's channel.

        Only reached for a session that still had an owner, which is the
        only case where another thread can be inside ``Channel.request``
        on this socket. That owner may be blocked on a read that will
        never complete: if reclaim tore a frame in half, its ``recv``
        loop can be waiting out a bogus multi-gigabyte length prefix,
        which is a hang rather than an error and so is invisible to
        every ``except`` on the path.

        ``shutdown``, not ``close``. Closing a socket does not reliably
        wake a ``recv`` already blocked on it in another thread — the fd
        just stops being valid, and on some platforms the reader sits
        there anyway. SHUT_RDWR delivers the EOF, which the reader turns
        into ``ChannelClosed`` and the owner sees as ``SessionLost``:
        the recovery it was promised, rather than a wedge.

        Doing it before ``close()`` also matches what this path is. A
        reclaim is a deliberate death, not a clean park, and EOF is
        already the designed kill signal — the guest powers off on it
        (syncing on the way), and the VMM exits with the guest. The
        graceful ``shutdown`` verb close() would otherwise send buys
        nothing here and can itself block on the same torn channel.
        """
        if getattr(session, "frozen", False):
            return  # a frozen park is files; there is no live channel
        sock = getattr(getattr(session, "_ch", None), "_sock", None)
        if sock is None:
            return
        try:
            sock.shutdown(socketlib.SHUT_RDWR)
        except OSError:
            pass  # already dead, or never connected: nothing to release

    def _teardown(self, session: VmSession, had_owner: bool = False) -> None:
        # A parked session already ran close() once (that's what parked
        # it), so clear both the pool hook AND the closed latch — else
        # close() no-ops and the VM process would leak.
        with self._lock:
            # `had_owner` is passed in rather than only inferred here,
            # because the one caller that matters cannot leave it to be
            # rediscovered: _make_room has to drop its victim from
            # _bound before it gets here, or the capacity arithmetic it
            # loops on never falls below the cap and it picks the same
            # victim forever. Inferring alone made the abort below dead
            # code on the exact path it was written for.
            had_owner = (self._bound.pop(id(session), None) is not None
                         or had_owner)
        if had_owner:
            # Idle victims (TTL, overflow, a park that failed to come
            # back) have no second thread and keep their graceful
            # poweroff. Only a reclaimed-from-an-owner session can have
            # a reader to release, so only it pays the abrupt exit.
            self._abort_channel(session)
        # Disposal is NOT a clean park: TTL expiry, overflow, reclaim,
        # and failed-reset all land here, and none of them may publish
        # scratch (a reclaimed VM was never quiesced; a TTL victim's
        # clone is staler than whatever parked since). Parked victims
        # already promoted at park time, so nothing true is lost.
        session._scratch_master = None
        session._pool = None
        session._closed = False
        try:
            session.close()
        except Exception:  # noqa: BLE001 — disposal must not leak a VM
            _log.debug("close failed during teardown", exc_info=True)

    def _reap_lost_locked(self) -> list[VmSession]:
        """Bound sessions whose wire has already failed, for the CALLER
        to tear down after releasing the lock (as ``_expire_locked``).

        Reclaiming one takes nothing from its owner: a lost session can
        never be used again — that is exactly what the latch in
        ``HostSession._request`` established — so this is garbage that
        happens to still be holding a VM. Emphatically NOT the
        ``max_total`` reclaim, which interrupts a *live* session and is
        logged as a loss somebody pays for.

        It exists because the only other thing that frees a bound
        session is its owner remembering to call ``close()``, and the
        moment they are most likely to forget is the recovery path,
        where the natural move is to rebind the variable to a fresh
        session. Dropping the reference frees nothing — the pool holds
        one too — so without this a wedged VM would keep its memory
        until the process exited.
        """
        lost = [s for s in self._bound.values() if getattr(s, "_lost", None)]
        for s in lost:
            self._bound.pop(id(s), None)
        return lost

    def _expire_locked(self) -> list[VmSession]:
        """Prune expired idle VMs; returns them for the CALLER to tear
        down after releasing the lock — close() does channel I/O and can
        wait seconds, which must not stall every acquire/release."""
        cutoff = time.monotonic() - self.ttl
        expired = []
        for key, bucket in self._idle.items():
            # Targeted keys keep their newest `n` regardless of age —
            # a prewarmed VM that expired quietly would resurrect the
            # exact first-touch boot prewarming exists to kill.
            floor = self._targets.get(key, (0, None))[0]
            keep, stale = [], []
            for t, tag, s in bucket:  # newest first
                (keep if (t >= cutoff or len(keep) < floor) else stale).append(
                    (t, tag, s)
                )
            expired.extend(s for _, _, s in stale)
            self._idle[key] = keep
        return expired


_shared: dict[type, VmPool] = {}
_shared_lock = threading.Lock()


def shared_pool(session_cls: type | None = None) -> VmPool:
    """The process-wide default pool for a rung (what DudExecutor
    uses); ``session_cls=None`` picks the host platform's VM rung.

    ``$DUD_VM_MAX_TOTAL`` caps RUNNING VMs (bound + hot-idle; frozen
    parks are files and don't count) with demand-driven reclaim; unset
    means uncapped — macOS pages out untouched guest memory, so idle
    VMs cost less than their headline size and a hard cap is opt-in.
    """
    session_cls = session_cls or _default_cls()
    with _shared_lock:
        pool = _shared.get(session_cls)
        if pool is None:
            cap = os.environ.get("DUD_VM_MAX_TOTAL")
            pool = _shared[session_cls] = VmPool(
                max_total=int(cap) if cap else None, session_cls=session_cls
            )
        return pool


def acquire_vfkit(**kwargs: Any) -> VmSession:
    """Acquire from the shared vfkit pool. ``close()`` parks it."""
    from .vfkit import VfkitSession

    return shared_pool(VfkitSession).acquire(**kwargs)
