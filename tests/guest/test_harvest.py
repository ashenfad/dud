"""Harvest: overlay upperdir -> wire (writes, deletes), scan-diff parity.

Real whiteouts need mknod and real opaque marks need trusted xattrs
(both root-only), so tests inject the predicates: a whiteout is a file
whose name is registered in a set, opaque dirs likewise. The traversal,
expansion, and parity logic under test is identical either way; the
default predicates are exercised live by the vfkit conformance run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dud.guest.staging import harvest


class Tree:
    """Builds an upper/snap pair with fake whiteout/opaque markers."""

    def __init__(self, root: Path):
        self.upper = root / "upper"
        self.snap = root / "snap"
        self.upper.mkdir()
        self.snap.mkdir()
        self._whiteouts: set[Path] = set()
        self._opaques: set[Path] = set()

    def snap_file(self, rel: str, data: str = "x"):
        p = self.snap / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data)

    def upper_file(self, rel: str, data: str = "y"):
        p = self.upper / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data)

    def whiteout(self, rel: str):
        p = self.upper / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
        self._whiteouts.add(p)

    def opaque(self, rel: str):
        p = self.upper / rel
        p.mkdir(parents=True, exist_ok=True)
        self._opaques.add(p)

    def harvest(self):
        return harvest(
            self.upper, self.snap,
            is_whiteout=lambda p, st: p in self._whiteouts,
            is_opaque=lambda p: p in self._opaques,
        )


@pytest.fixture
def tree(tmp_path):
    return Tree(tmp_path)


def test_new_and_changed_files_are_writes(tree):
    tree.snap_file("a.txt", "old")
    tree.upper_file("a.txt", "new")
    tree.upper_file("b/c.txt")
    writes, deletes = tree.harvest()
    assert writes == ["a.txt", "b/c.txt"] and deletes == []


def test_identical_copy_up_is_not_a_write(tree):
    """Metadata-only copy-ups (touch/chmod) must not report as writes."""
    tree.snap_file("same.txt", "content")
    tree.upper_file("same.txt", "content")
    writes, deletes = tree.harvest()
    assert writes == [] and deletes == []


def test_whiteout_on_file_is_a_delete(tree):
    tree.snap_file("gone.txt")
    tree.whiteout("gone.txt")
    writes, deletes = tree.harvest()
    assert writes == [] and deletes == ["gone.txt"]


def test_whiteout_without_lower_counterpart_is_noop(tree):
    """Create-then-delete inside one stage: nothing crossed the wire."""
    tree.whiteout("ephemeral.txt")
    writes, deletes = tree.harvest()
    assert writes == [] and deletes == []


def test_whiteout_on_dir_expands_to_per_file_deletes(tree):
    tree.snap_file("d/a.txt")
    tree.snap_file("d/sub/b.txt")
    tree.whiteout("d")
    writes, deletes = tree.harvest()
    assert writes == [] and deletes == ["d/a.txt", "d/sub/b.txt"]


def test_opaque_dir_deletes_hidden_files_keeps_rewritten(tree):
    tree.snap_file("d/keep.txt", "kept")
    tree.snap_file("d/lost.txt")
    tree.opaque("d")
    tree.upper_file("d/keep.txt", "kept-rewritten")
    tree.upper_file("d/new.txt")
    writes, deletes = tree.harvest()
    assert writes == ["d/keep.txt", "d/new.txt"]
    assert deletes == ["d/lost.txt"]


def test_opaque_dir_identical_rewrite_neither_writes_nor_deletes(tree):
    tree.snap_file("d/same.txt", "v")
    tree.opaque("d")
    tree.upper_file("d/same.txt", "v")
    writes, deletes = tree.harvest()
    assert writes == [] and deletes == []


def test_whiteouts_inside_opaque_scope_are_ignored(tree):
    tree.snap_file("d/old.txt")
    tree.opaque("d")
    tree.whiteout("d/old.txt")  # redundant: opaque already hides lower
    writes, deletes = tree.harvest()
    assert writes == [] and deletes == ["d/old.txt"]


def test_dir_to_file_transition(tree):
    """Replacing a dir with a file deletes the dir's files."""
    tree.snap_file("thing/a.txt")
    tree.snap_file("thing/b.txt")
    tree.upper_file("thing", "now a file")
    writes, deletes = tree.harvest()
    assert writes == ["thing"]
    assert deletes == ["thing/a.txt", "thing/b.txt"]


def test_file_to_dir_transition(tree):
    """Replacing a file with a dir (goes opaque) deletes the file."""
    tree.snap_file("thing", "was a file")
    tree.opaque("thing")
    tree.upper_file("thing/inner.txt")
    writes, deletes = tree.harvest()
    assert writes == ["thing/inner.txt"] and deletes == ["thing"]


def test_ignore_rules_apply_to_writes_and_deletes(tree):
    tree.snap_file("mod.pyc")
    tree.snap_file("__pycache__/mod.cpython-312.pyc")
    tree.upper_file("pkg/__pycache__/x.pyc")
    tree.upper_file("junk.pyo")
    tree.whiteout("mod.pyc")
    writes, deletes = tree.harvest()
    assert writes == [] and deletes == []


def test_nested_opaque_within_opaque_no_double_deletes(tree):
    tree.snap_file("a/b/deep.txt")
    tree.opaque("a")
    tree.opaque("a/b")
    writes, deletes = tree.harvest()
    assert writes == [] and deletes == ["a/b/deep.txt"]


def _upper_symlink(tree, rel, target="wherever"):
    p = tree.upper / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.symlink_to(target)


def test_symlink_shadowing_lower_file_reports_delete(tree):
    """scan-diff sees the lower file vanish from the merged index."""
    tree.snap_file("data.txt")
    _upper_symlink(tree, "data.txt")
    writes, deletes = tree.harvest()
    assert writes == [] and deletes == ["data.txt"]


def test_symlink_shadowing_lower_dir_expands_deletes(tree):
    tree.snap_file("d/a.txt")
    tree.snap_file("d/b.txt")
    _upper_symlink(tree, "d")
    writes, deletes = tree.harvest()
    assert writes == [] and deletes == ["d/a.txt", "d/b.txt"]


def test_symlink_with_no_lower_counterpart_is_silent(tree):
    _upper_symlink(tree, "fresh-link")
    writes, deletes = tree.harvest()
    assert writes == [] and deletes == []


def test_symlink_over_lower_symlink_is_silent(tree):
    """Lower symlinks were never in the baseline index — no delete."""
    (tree.snap / "ln").symlink_to("a")
    _upper_symlink(tree, "ln", "b")
    writes, deletes = tree.harvest()
    assert writes == [] and deletes == []
