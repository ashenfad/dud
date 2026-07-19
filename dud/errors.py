"""The exception spine: every public dud failure descends from DudError.

Concrete errors keep their historical bases too (SessionLost is still
a RuntimeError, NotRepresentable still a ValueError), so existing
``except`` clauses keep working — DudError is an additional handle
(``except dud.DudError``), not a migration.
"""


class DudError(Exception):
    """Base for all public dud errors."""


class SessionLost(DudError, RuntimeError):
    """The guest went away mid-request (VM died, channel EOF/reset).

    The session object is unusable afterward. Recovery is the owner's
    move — dud never holds the authoritative workspace tree, so only
    the layer above can reopen a session and re-push state (see the
    disposable thesis: any VM may vanish at any moment; DudExecutor's
    recovery path is acquire + push + retry-once). Raised in place of
    the transport-level errors so consumers write one ``except``, not
    a taxonomy of socket failures.
    """


class IsolationUnavailable(DudError, RuntimeError):
    """The requested VM rung can't run here (platform/tooling/kernel).

    Cross-rung by design: vfkit raises it on non-macOS, firecracker
    will raise it without KVM. It lives here — not in a backend
    module — so no rung's error is another rung's import.
    """
