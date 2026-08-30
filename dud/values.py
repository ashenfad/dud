"""The Value codec: what may cross the guest/host boundary.

Tagged forms, JSON floor, no live objects, no pickle. See DESIGN.md
"Outputs: emits, not namespaces". v0 carries three tags:

- ``{"t": "json", "v": ...}``   — any JSON-representable value
- ``{"t": "bytes", "mime": m, "b64": ...}`` — small binary, base64
  (large payloads belong in the workspace as files, not on the wire)
- ``{"t": "file", "path": p}``  — a workspace-relative path reference

``chart``/``table`` ride as ``json`` with conventions until proven
worth first-classing (PLAN.md decision #3).
"""

from __future__ import annotations

import base64
import json
from typing import Any

from .errors import DudError


class NotRepresentable(DudError, ValueError):
    """Value has no codec form. Callers decide: skip+record, or raise."""


class ValueTooLarge(NotRepresentable):
    """Value has a codec form, but one too big to put on the wire.

    A subclass of :class:`NotRepresentable` so that everything already
    written to skip values that cannot cross also skips values that
    should not — the harvest path records both in ``outputs_skipped``,
    and neither is a reason to fail an exec. The distinct type is for
    callers that want to tell "there is no encoding for this" from
    "there is one, and it is enormous", which are different problems
    with different fixes.
    """


def _mib(n: int) -> str:
    return f"{n / (1 << 20):.1f} MiB"


_REPORT_KEY_MAX = 64


def _report_key(k: str) -> str:
    """The name to file a skip under.

    Truncated, because ``skipped`` rides the very frame the caller is
    being told about: reporting a 40 MB binding name under itself would
    put that name on the wire anyway, and the guard would be reporting
    the problem by causing it. Long names collapse together, which is
    the right trade for input nobody legitimately produces.
    """
    if len(k) <= _REPORT_KEY_MAX:
        return k
    return k[: _REPORT_KEY_MAX - 3] + "..."


def _encode_sized(v: Any) -> tuple[dict, int]:
    """``(tagged form, its size on the wire)``.

    The size is taken from the representability probe rather than
    measured separately: ``json.dumps`` has to run anyway to learn
    whether a value *can* be encoded, and its length is then free. It
    is an estimate — the tagged wrapper adds a few bytes and the real
    serialization happens once this value is nested into a frame — but
    it is exact where it matters, because the payload dominates
    everything around it by orders of magnitude at any size worth
    refusing.

    (The probe's output is genuinely thrown away, and cannot be
    reused: what goes on the wire is this value nested inside a larger
    message, so ``_send_msg`` serializes it again. Dropping the probe
    would mean one bad value failing a whole frame with nothing to say
    which key caused it, which is worse than the second pass.)
    """
    if isinstance(v, bytearray):
        v = bytes(v)
    if isinstance(v, bytes):
        b64 = base64.b64encode(v).decode()
        return ({"t": "bytes", "mime": "application/octet-stream",
                 "b64": b64}, len(b64))
    try:
        probe = json.dumps(v)
    except (TypeError, ValueError):
        raise NotRepresentable(type(v).__name__) from None
    return {"t": "json", "v": v}, len(probe)


def encode_value(v: Any, cap: int | None = None) -> dict:
    """Tag a value for the wire, optionally refusing an oversized one.

    ``cap`` is a ceiling in wire bytes; over it, :class:`ValueTooLarge`.
    Guest-side callers pass one because everything they encode transits
    the supervisor — PID 1 on a VM rung — which parses it whole. Left
    unset the behavior is exactly as before, which is what the host
    side wants: a value the *host* chose to send is not a payload
    anyone needs protecting from.
    """
    tagged, size = _encode_sized(v)
    if cap is not None and size > cap:
        raise ValueTooLarge(
            f"{type(v).__name__} is {_mib(size)} on the wire, over the "
            f"{_mib(cap)} limit; write it to a workspace file instead"
        )
    return tagged


def file_ref(path: str) -> dict:
    return {"t": "file", "path": path}


def decode_value(tagged: dict) -> Any:
    t = tagged.get("t")
    if t == "json":
        return tagged.get("v")
    if t == "bytes":
        return base64.b64decode(tagged.get("b64", ""))
    if t == "file":
        # Decodes to the path string; the consumer resolves it against
        # the workspace root. Deliberately not auto-read: whether and
        # when to load the content is the consumer's trust decision.
        return tagged.get("path", "")
    raise NotRepresentable(f"unknown tag {t!r}")


def encode_map(
    d: dict[str, Any], cap: int | None = None, total: int | None = None
) -> tuple[dict[str, dict], dict[str, str]]:
    """Encode a name->value dict. Returns ``(encoded, skipped)``, where
    ``skipped`` maps each name that could not cross to why.

    ``cap`` bounds one value, ``total`` the sum of those kept. Both are
    optional and both, when exceeded, skip rather than truncate: half a
    JSON document is not a smaller answer, it is a wrong one, and the
    name still appears in ``skipped`` saying what happened.

    A value that does not fit the remaining total is skipped on its
    own — the walk does not stop there. A 40 MiB array should not cost
    the caller the three-element list defined after it, and the
    alternative makes what you get back depend on the order bindings
    happen to appear in.
    """
    out: dict[str, dict] = {}
    skipped: dict[str, str] = {}
    used = 0
    for k, v in d.items():
        # The name is on the wire too, and nothing else was measuring
        # it: `globals()['k' * 40_000_000] = 1` charged one byte to the
        # total and put 40 MB in the frame. Counting it costs nothing
        # and closes a hole that a value-shaped guard cannot see.
        kbytes = len(k.encode()) + 3  # the quotes and the colon ride along
        try:
            tagged, size = _encode_sized(v)
        except NotRepresentable:
            skipped[_report_key(k)] = type(v).__name__
            continue
        size += kbytes
        if cap is not None and size > cap:
            skipped[_report_key(k)] = (
                f"{type(v).__name__} ({_mib(size)} exceeds the "
                f"{_mib(cap)} per-value limit)")
            continue
        if total is not None and used + size > total:
            skipped[_report_key(k)] = (
                f"{type(v).__name__} ({_mib(size)} would exceed "
                f"the {_mib(total)} total)")
            continue
        out[k] = tagged
        used += size
    return out, skipped


def decode_map(d: dict[str, dict]) -> dict[str, Any]:
    return {k: decode_value(v) for k, v in d.items()}
