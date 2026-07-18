"""Guest-side flattening of rich ``ui`` values to workspace files.

The ``ui = {name: value}`` convention turns values into ``/ui/<name>.<ext>``
artifacts. Values that fit dud's json/bytes codec (dicts of stats/callouts,
image bytes, path strings, plain json) cross the wire and the host renders
them. RICH live objects — a plotly ``Figure``, a pandas ``DataFrame``, a
matplotlib figure, a PIL image — can't cross, so we serialize them here,
where the libraries and the objects live, writing the *same* files the host
would. They ride back as ordinary workspace writes and the host adopts any
new ``/ui`` file, so nothing above dud has to change.

Only the rich types live here; everything representable stays the host
renderer's job (one authority for the shape rules). Detection is duck-typed
by module name + method, so dud keeps its zero third-party dependencies.
"""

from __future__ import annotations

import io
import json
import os
from typing import Any

# Parity with the host renderer's artifact cap (adapters/render.py).
_MAX_ARTIFACT_BYTES = 8 * 1024 * 1024


def flatten_rich(ui: dict, workspace: str) -> set[str]:
    """Write rich ``ui`` values under ``<workspace>/ui/``.

    Returns the names it handled so the caller can drop them from ``ui``
    before harvest — the representable remainder still crosses to the host.
    """
    handled: set[str] = set()
    for name, value in list(ui.items()):
        try:
            rel = _materialize(str(name), value, workspace)
        except Exception:
            rel = None  # a rich value that failed to serialize: leave it be
        if rel is not None:
            handled.add(name)
    return handled


def _write(workspace: str, relpath: str, data: bytes) -> str | None:
    if len(data) > _MAX_ARTIFACT_BYTES:
        return None  # too large: skip (host renderer caps identically)
    full = os.path.join(workspace, relpath)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(data)
    return relpath


def _materialize(name: str, value: Any, workspace: str) -> str | None:
    """One rich value -> one ``/ui`` file. None if not a rich type."""
    mod = type(value).__module__ or ""

    if mod.startswith("plotly") and hasattr(value, "to_json"):
        return _write(workspace, f"ui/{name}.plotly.json", value.to_json().encode())

    if mod.startswith("pandas") and hasattr(value, "columns"):
        total = len(value)
        payload = json.loads(
            value.head(200).to_json(orient="split", date_format="iso")
        )
        payload["total"] = total
        return _write(workspace, f"ui/{name}.table.json",
                      json.dumps(payload).encode())

    if mod.startswith("matplotlib") and hasattr(value, "savefig"):
        buf = io.BytesIO()
        value.savefig(buf, format="png", bbox_inches="tight")
        return _write(workspace, f"ui/{name}.png", buf.getvalue())

    if mod.startswith("PIL") and hasattr(value, "save"):
        buf = io.BytesIO()
        value.save(buf, format="PNG")
        return _write(workspace, f"ui/{name}.png", buf.getvalue())

    return None  # representable / unknown: let it cross to the host renderer
