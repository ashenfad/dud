"""replay(): rebase folding of an upperdir onto the snapshot, offline.

Same injected-predicate trick as the harvest tests — real whiteouts and
opaque xattrs need root; the folding logic is identical either way and
the live vfkit conformance run covers the real markers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dud.guest.staging import replay


class Fold:
    def __init__(self, root: Path):
        self.upper = root / "upper"
        self.snap = root / "snap"
        self.upper.mkdir()
        self.snap.mkdir()
        self._whiteouts: set[Path] = set()
        self._opaques: set[Path] = set()

    def snap_file(self, rel, data="x"):
        p = self.snap / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data)

    def upper_file(self, rel, data="y"):
        p = self.upper / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data)

    def whiteout(self, rel):
        p = self.upper / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
        self._whiteouts.add(p)

    def opaque(self, rel):
        p = self.upper / rel
        p.mkdir(parents=True, exist_ok=True)
        self._opaques.add(p)

    def run(self):
        replay(
            self.upper, self.snap,
            is_whiteout=lambda p, st: p in self._whiteouts,
            is_opaque=lambda p: p in self._opaques,
        )

    def tree(self):
        return {
            str(p.relative_to(self.snap)): p.read_text()
            for p in sorted(self.snap.rglob("*")) if p.is_file()
        }


@pytest.fixture
def fold(tmp_path):
    return Fold(tmp_path)


def test_files_copy_up_over_and_beside(fold):
    fold.snap_file("keep.txt", "kept")
    fold.snap_file("mod.txt", "old")
    fold.upper_file("mod.txt", "new")
    fold.upper_file("added/x.txt", "x")
    fold.run()
    assert fold.tree() == {
        "keep.txt": "kept", "mod.txt": "new", "added/x.txt": "x",
    }


def test_whiteout_deletes_file_and_dir(fold):
    fold.snap_file("gone.txt")
    fold.snap_file("d/inner.txt")
    fold.whiteout("gone.txt")
    fold.whiteout("d")
    fold.run()
    assert fold.tree() == {}


def test_opaque_dir_replaces_wholesale(fold):
    fold.snap_file("d/lost.txt")
    fold.snap_file("d/sub/also-lost.txt")
    fold.opaque("d")
    fold.upper_file("d/fresh.txt", "f")
    fold.run()
    assert fold.tree() == {"d/fresh.txt": "f"}


def test_dir_to_file_and_file_to_dir_transitions(fold):
    fold.snap_file("was-dir/x.txt")
    fold.upper_file("was-dir", "now file")
    fold.snap_file("was-file", "flat")
    fold.opaque("was-file")  # mkdir over whiteout goes opaque
    fold.upper_file("was-file/inner.txt", "deep")
    fold.run()
    assert fold.tree() == {
        "was-dir": "now file", "was-file/inner.txt": "deep",
    }


def test_replay_then_empty_upper_is_idempotent_baseline(fold):
    """After folding, an empty upper folds to no further change."""
    fold.snap_file("a.txt", "1")
    fold.upper_file("a.txt", "2")
    fold.run()
    before = fold.tree()
    for p in list(fold.upper.rglob("*")):
        if p.is_file():
            p.unlink()
    fold.run()
    assert fold.tree() == before == {"a.txt": "2"}