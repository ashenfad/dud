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
import os
import threading
import time
from typing import Any

from .vfkit import VfkitSession

# Host-side binding kwargs: per-session state rebound on reuse, never
# part of the VM's identity.
_BINDING_KEYS = ("host_objects", "allow", "cache", "on_emit")
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
        (session_cls or VfkitSession).__init__
    ).parameters
    ident: dict[str, Any] = {}
    for name, p in params.items():
        if name == "self" or name in _BINDING_KEYS or name in _NON_IDENTITY:
            continue
        default = None if p.default is inspect.Parameter.empty else p.default
        ident[name] = kwargs.get(name, default)
    return json.dumps(ident, sort_keys=True, default=str)


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
    ):
        self.max_idle = max_idle
        self.ttl = ttl
        self.max_total = max_total
        self.session_cls = session_cls or VfkitSession
        self._idle: dict[str, list[tuple[float, VfkitSession]]] = {}
        # Bound = checked out and held by a session owner. Tracked so
        # max_total can reclaim the LRU one under demand (id() keys:
        # sessions aren't hashable-by-value and identity is the point).
        self._bound: dict[int, VfkitSession] = {}
        self._targets: dict[str, tuple[int, dict[str, Any]]] = {}
        self._filling: set[str] = set()
        self._lock = threading.Lock()
        atexit.register(self.close)

    # ---- lifecycle ----------------------------------------------------

    def acquire(self, state: str | None = None, **kwargs: Any) -> VfkitSession:
        """Hand out a VM for this config; prefer one parked with tag
        ``state`` (content-addressed workspace identity, e.g. a kvgit
        commit). On a tag match the returned session has
        ``resumed=True`` — its tree already IS that state, so the caller
        skips the push and just continues. Any other VM (or a fresh
        boot) comes back ``resumed=False``."""
        key = _fingerprint(kwargs, self.session_cls)
        binding = {k: kwargs.get(k) for k in _BINDING_KEYS}
        while True:
            matched = False
            with self._lock:
                stale = self._expire_locked()
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
            for s in stale:
                self._teardown(s)
            if parked is None:
                self._make_room()
                self._maybe_refill(key)  # replace what we're about to boot
                session = self.session_cls(**kwargs)
                session._pool = self  # close() -> release
                session.resumed = False
                with self._lock:
                    self._bound[id(session)] = session
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
            except Exception:
                self._teardown(session)
                continue  # dead while parked: boot fresh next loop
            self._maybe_refill(key)  # top the level back up in background
            self._rebind(session, binding)
            session.resumed = matched
            with self._lock:
                self._bound[id(session)] = session
            return session

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
                # Frozen parks are files, not processes: they consume
                # no RAM/CPU, so the cap neither counts them nor
                # reclaims them (TTL is their only expiry — disk GC).
                total = len(self._bound) + sum(
                    1
                    for b in self._idle.values()
                    for _, _, s in b
                    if not getattr(s, "frozen", False)
                )
                if total < self.max_total:
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
                else:
                    quiet = [
                        s for s in self._bound.values()
                        if getattr(s, "_in_flight", 0) == 0
                    ]
                    if not quiet:
                        return  # all mid-request: over-boot, don't block
                    victim = min(
                        quiet, key=lambda s: getattr(s, "last_used", 0.0)
                    )
                    self._bound.pop(id(victim), None)
            self._teardown(victim)

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
                except Exception:
                    return  # best-effort: no kernel / no KVM -> no prewarm
                # Zero-RAM prewarm where the backend can: a frozen
                # freshly-booted VM is warmth as a file.
                try:
                    if hasattr(session, "freeze"):
                        session.freeze()
                except Exception:
                    try:
                        session.close()
                    except Exception:
                        pass
                    return
                session._pool = self
                with self._lock:
                    self._idle.setdefault(key, []).insert(
                        0, (time.monotonic(), None, session)
                    )
        finally:
            with self._lock:
                self._filling.discard(key)

    def release(self, session: VfkitSession) -> None:
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
            session._ch.request("reset_guest", {"keep_tree": bool(state)})
        except Exception:
            self._teardown(session)
            return
        # A successful reset means the guest is alive and synced: an
        # intermediate scratch promotion here means the cache survives
        # even if the parked VM later dies. Best-effort by the scratch
        # contract — a failed promotion is a cold cache, not an error.
        try:
            session.promote_scratch()
        except Exception:
            pass
        # Frozen posture: park as files, not as a process. The reset
        # already ran, so what freezes is a clean guest; thaw at the
        # next acquire resumes it in tens of ms with imports warm.
        if hasattr(session, "freeze"):
            try:
                session.freeze()
            except Exception:
                self._teardown(session)
                return
        key = _fingerprint(session._pool_kwargs, self.session_cls)
        with self._lock:
            stale = self._expire_locked()
            bucket = self._idle.setdefault(key, [])
            bucket.insert(0, (time.monotonic(), state, session))
            limit = max(self.max_idle, self._targets.get(key, (0, None))[0])
            overflow = bucket[limit:]
            del bucket[limit:]
        for _, _, s in overflow:
            self._teardown(s)
        for s in stale:
            self._teardown(s)

    def close(self) -> None:
        with self._lock:
            buckets, self._idle = self._idle, {}
        for bucket in buckets.values():
            for _, _, s in bucket:
                self._teardown(s)

    # ---- internals ----------------------------------------------------

    def _rebind(self, session: VfkitSession, binding: dict[str, Any]) -> None:
        session.cache = binding["cache"] if binding["cache"] is not None else {}
        session.host_objects = binding["host_objects"] or {}
        session.allow = binding["allow"] or {}
        session.on_emit = binding["on_emit"]
        session.emits = []
        session._closed = False

    def _teardown(self, session: VfkitSession) -> None:
        # A parked session already ran close() once (that's what parked
        # it), so clear both the pool hook AND the closed latch — else
        # close() no-ops and the VM process would leak.
        with self._lock:
            self._bound.pop(id(session), None)
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
        except Exception:
            pass

    def _expire_locked(self) -> list[VfkitSession]:
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
    if session_cls is None:
        import platform

        if platform.system() == "Darwin":
            session_cls = VfkitSession
        else:
            from .firecracker import FirecrackerSession

            session_cls = FirecrackerSession
    with _shared_lock:
        pool = _shared.get(session_cls)
        if pool is None:
            cap = os.environ.get("DUD_VM_MAX_TOTAL")
            pool = _shared[session_cls] = VmPool(
                max_total=int(cap) if cap else None, session_cls=session_cls
            )
        return pool


def acquire_vfkit(**kwargs: Any) -> VfkitSession:
    """Acquire from the shared vfkit pool. ``close()`` parks it."""
    return shared_pool(VfkitSession).acquire(**kwargs)
