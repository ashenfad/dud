from pathlib import Path

from dud.diffscan import extract_tar, make_tar, scan_diff, sync_copy


def _seed(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


def test_scan_diff_detects_writes_and_deletes(tmp_path):
    work, base = tmp_path / "work", tmp_path / "base"
    _seed(work, {"keep.txt": "same", "mod.txt": "old", "gone.txt": "x"})
    sync_copy(work, base)

    (work / "mod.txt").write_text("new")
    (work / "gone.txt").unlink()
    _seed(work, {"sub/new.txt": "added"})

    writes, deletes = scan_diff(work, base)
    assert writes == ["mod.txt", "sub/new.txt"]
    assert deletes == ["gone.txt"]


def test_tar_roundtrip(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _seed(src, {"a.txt": "alpha", "d/b.txt": "beta"})
    data = make_tar(src, ["a.txt", "d/b.txt"])
    extract_tar(data, dst)
    assert (dst / "a.txt").read_text() == "alpha"
    assert (dst / "d" / "b.txt").read_text() == "beta"


def test_identical_trees_diff_empty(tmp_path):
    work, base = tmp_path / "work", tmp_path / "base"
    _seed(work, {"x.txt": "1"})
    sync_copy(work, base)
    assert scan_diff(work, base) == ([], [])


def test_bytecode_is_invisible_to_diffs(tmp_path):
    """Importing a workspace module writes __pycache__/*.pyc — derived
    state that must never appear as a write (it would poison read-only
    views) nor as a delete after a reset."""
    work, base = tmp_path / "work", tmp_path / "base"
    _seed(work, {"app/api/_helpers.py": "X = 1"})
    sync_copy(work, base)

    _seed(work, {"app/api/__pycache__/_helpers.cpython-312.pyc": "\x00fake",
                 "stray.pyc": "\x00also fake"})
    assert scan_diff(work, base) == ([], [])

    # a REAL write next to the bytecode still shows through
    _seed(work, {"app/api/new.py": "Y = 2"})
    assert scan_diff(work, base) == (["app/api/new.py"], [])
