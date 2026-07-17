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


class NotRepresentable(ValueError):
    """Value has no codec form. Callers decide: skip+record, or raise."""


def encode_value(v: Any) -> dict:
    if isinstance(v, bytes):
        return {"t": "bytes", "mime": "application/octet-stream",
                "b64": base64.b64encode(v).decode()}
    if isinstance(v, bytearray):
        return encode_value(bytes(v))
    try:
        json.dumps(v)
    except (TypeError, ValueError):
        raise NotRepresentable(type(v).__name__) from None
    return {"t": "json", "v": v}


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


def encode_map(d: dict[str, Any]) -> tuple[dict[str, dict], dict[str, str]]:
    """Encode a name->value dict. Returns (encoded, skipped) where
    skipped maps names that had no codec form to their type names."""
    out: dict[str, dict] = {}
    skipped: dict[str, str] = {}
    for k, v in d.items():
        try:
            out[k] = encode_value(v)
        except NotRepresentable:
            skipped[k] = type(v).__name__
    return out, skipped


def decode_map(d: dict[str, dict]) -> dict[str, Any]:
    return {k: decode_value(v) for k, v in d.items()}
