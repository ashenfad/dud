"""Publishing files into the shared artifact cache under ``~/.dud``.

Registry blobs, pinned debs, rootfs images, kernels and scratch masters
are all written by whichever caller reaches for them first and read by
everyone after. Concurrent writers of one artifact are routine rather
than exotic: the pool's background refill boots a session on the same
key a foreground acquire is already booting, and two processes sharing
a home have no coordination at all.

Two rules make that race harmless, and both live here so no cache site
has to remember them:

- **One staging path per writer.** A shared ``<name>.part`` is what
  turns a race destructive — both writers open it ``"wb"`` and write
  from offset 0, so the bytes on disk interleave while each writer's
  own digest check still passes on the bytes it *sent*. A torn file
  then gets published under a content-addressed name and, because the
  cache trusts its own filenames, is never verified again.
- **Publish with :func:`os.replace`.** Last writer wins, and a reader
  sees either the previous complete artifact or the new one, never a
  partial file.

Failure leaves nothing behind: the staging file is removed whether the
write succeeded, raised, or verified wrong.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def part_path(dest: Path, tag: str = "part") -> Path:
    """A staging path beside ``dest``, unique to this writer.

    Beside rather than in a temp dir: the publish step must be a rename
    within one filesystem, and callers stage artifacts (sparse images,
    reflinked clones) whose properties a cross-device copy would lose.
    """
    return dest.with_name(
        f"{dest.name}.{tag}.{os.getpid()}.{threading.get_ident():x}"
    )


@contextmanager
def staged(dest: Path, tag: str = "part") -> Iterator[Path]:
    """Yield a writer-private path; publish it as ``dest`` on clean exit.

    ::

        with staged(dest) as tmp:
            tmp.write_bytes(data)      # dest still holds the old artifact
        # dest is now the new artifact, in one atomic step

    Raising skips the publish, so a failed or digest-mismatched write
    never becomes visible to another reader.
    """
    tmp = part_path(dest, tag)
    try:
        yield tmp
        os.replace(tmp, dest)
    finally:
        # A published tmp is already gone; this catches every other exit.
        tmp.unlink(missing_ok=True)


def write_json(dest: Path, obj: Any, **dumps_kwargs: Any) -> None:
    """Atomically publish ``obj`` as JSON at ``dest``.

    Metadata sidecars are small enough that a torn write looks unlikely,
    but they are read back with :func:`json.loads` — so a torn one is a
    hard parse failure, not a slightly-wrong value.
    """
    with staged(dest) as tmp:
        tmp.write_text(json.dumps(obj, **dumps_kwargs))
