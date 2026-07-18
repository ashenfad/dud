"""Reuse vfkit VMs across sessions: same image, new state, no boot.

The design premise makes VMs fungible — files ride in via ``push_tree``,
cache and host objects live host-side, python state dies with each
runner — so a session's identity never touches the machine. A pool
keyed by the *boot fingerprint* (image, packages, kernel, sizing) hands
an idle VM to the next session for the cost of a ``reset_guest`` +
``push_tree`` (~100s of ms) instead of a boot (~seconds).

Hygiene on release, not acquire (secrets leave promptly): wipe both
trees, restore boot-time shell env, and kill every non-supervisor
process in the guest (see ``Supervisor.do_reset_guest``). Residue
*outside* the workspace (``/tmp``, absolute-path writes, warmed
``__pycache__``) survives reuse — acceptable within one user's studio,
and the warmed imports are a feature; overlay-at-root is the eventual
stricter reset (see ROADMAP).

Scope: in-process only. A parked VM still dies with this process (the
guest powers off when its channel drops); surviving host restarts is
the separate detach/reconnect item.
"""

from __future__ import annotations

import atexit
import inspect
import json
import threading
import time
from typing import Any

from .vfkit import VfkitSession

# Host-side binding kwargs: per-session state rebound on reuse, never
# part of the VM's identity.
_BINDING_KEYS = ("host_objects", "allow", "cache", "on_emit")
# Constructor kwargs that don't change what was booted.
_NON_IDENTITY = ("boot_timeout",)


def _fingerprint(kwargs: dict[str, Any]) -> str:
    """Boot-identity hash, normalized against the constructor's defaults
    so sparse call-site kwargs and a session's fully-captured
    ``_pool_kwargs`` produce the SAME key (acquire must find what release
    parked)."""
    params = inspect.signature(VfkitSession.__init__).parameters
    ident: dict[str, Any] = {}
    for name, p in params.items():
        if name == "self" or name in _BINDING_KEYS or name in _NON_IDENTITY:
            continue
        default = None if p.default is inspect.Parameter.empty else p.default
        ident[name] = kwargs.get(name, default)
    return json.dumps(ident, sort_keys=True, default=str)


class VmPool:
    """Idle vfkit VMs keyed by boot fingerprint.

    ``acquire`` returns a :class:`VfkitSession` whose ``close()`` parks
    the VM here (after guest reset) instead of powering it off; the pool
    tears VMs down on idle-cap overflow, TTL expiry (checked lazily),
    ``close()``, or process exit.
    """

    def __init__(self, max_idle: int = 2, ttl: float = 900.0):
        self.max_idle = max_idle
        self.ttl = ttl
        self._idle: dict[str, list[tuple[float, VfkitSession]]] = {}
        self._targets: dict[str, tuple[int, dict[str, Any]]] = {}
        self._filling: set[str] = set()
        self._lock = threading.Lock()
        atexit.register(self.close)

    # ---- lifecycle ----------------------------------------------------

    def acquire(self, **kwargs: Any) -> VfkitSession:
        key = _fingerprint(kwargs)
        binding = {k: kwargs.get(k) for k in _BINDING_KEYS}
        while True:
            with self._lock:
                self._expire_locked()
                bucket = self._idle.get(key) or []
                parked = bucket.pop() if bucket else None
            if parked is None:
                self._maybe_refill(key)  # replace what we're about to boot
                session = VfkitSession(**kwargs)
                session._pool = self  # close() -> release
                return session
            _, session = parked
            try:
                session.ping()
            except Exception:
                self._teardown(session)
                continue  # dead while parked: boot fresh next loop
            self._maybe_refill(key)  # top the level back up in background
            self._rebind(session, binding)
            return session

    def prewarm(self, n: int, background: bool = True, **kwargs: Any) -> None:
        """Keep ``n`` idle VMs warm for this config: boot-and-park the
        deficit now (in a background thread by default), and re-fill
        whenever an acquire drains below ``n``. Targeted VMs are exempt
        from TTL expiry — holding them warm is the entire point. Callers
        opting in accept the idle RAM cost."""
        key = _fingerprint(kwargs)
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
                try:
                    session = VfkitSession(**boot_kwargs)
                except Exception:
                    return  # best-effort: no kernel / no HVF -> no prewarm
                session._pool = self
                with self._lock:
                    self._idle.setdefault(key, []).insert(
                        0, (time.monotonic(), session)
                    )
        finally:
            with self._lock:
                self._filling.discard(key)

    def release(self, session: VfkitSession) -> None:
        """Reset the guest and park; a VM that fails reset is torn down."""
        try:
            session._ch.request("reset_guest")
        except Exception:
            self._teardown(session)
            return
        key = _fingerprint(session._pool_kwargs)
        with self._lock:
            self._expire_locked()
            bucket = self._idle.setdefault(key, [])
            bucket.insert(0, (time.monotonic(), session))
            limit = max(self.max_idle, self._targets.get(key, (0, None))[0])
            overflow = bucket[limit:]
            del bucket[limit:]
        for _, s in overflow:
            self._teardown(s)

    def close(self) -> None:
        with self._lock:
            buckets, self._idle = self._idle, {}
        for bucket in buckets.values():
            for _, s in bucket:
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
        session._pool = None
        session._closed = False
        try:
            session.close()
        except Exception:
            pass

    def _expire_locked(self) -> None:
        cutoff = time.monotonic() - self.ttl
        expired = []
        for key, bucket in self._idle.items():
            # Targeted keys keep their newest `n` regardless of age —
            # a prewarmed VM that expired quietly would resurrect the
            # exact first-touch boot prewarming exists to kill.
            floor = self._targets.get(key, (0, None))[0]
            keep, stale = [], []
            for t, s in bucket:  # newest first
                (keep if (t >= cutoff or len(keep) < floor) else stale).append((t, s))
            expired.extend(s for _, s in stale)
            self._idle[key] = keep
        if expired:
            # teardown outside the lock is nicer, but expiry is rare and
            # close() only touches the session's own resources
            for s in expired:
                self._teardown(s)


_shared: VmPool | None = None
_shared_lock = threading.Lock()


def shared_pool() -> VmPool:
    """The process-wide default pool (what DudExecutor uses)."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = VmPool()
        return _shared


def acquire_vfkit(**kwargs: Any) -> VfkitSession:
    """Acquire from the shared pool. The session's ``close()`` parks it."""
    return shared_pool().acquire(**kwargs)
