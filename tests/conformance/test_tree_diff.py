"""Conformance: push_tree / pull_diff / reset — the state seam."""


def _seed(tmp_path, files):
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return tmp_path


def test_push_dir_materializes(session, tmp_path):
    session.push_dir(_seed(tmp_path / "t", {"data/in.csv": "a,b\n1,2\n"}))
    r = session.shell("cat data/in.csv")
    assert r.transcript == "a,b\n1,2\n"


def test_diff_captures_writes_and_deletes(session, tmp_path):
    session.push_dir(_seed(tmp_path / "t", {"keep.txt": "k", "gone.txt": "g"}))
    session.shell("echo new > added.txt && rm gone.txt && echo mod >> keep.txt")
    d = session.diff()
    assert set(d.writes) == {"added.txt", "keep.txt"}
    assert d.writes["added.txt"] == b"new\n"
    assert d.deletes == ["gone.txt"]


def test_diff_without_rebase_repeats(session, tmp_path):
    session.push_dir(_seed(tmp_path / "t", {}))
    session.shell("echo x > f.txt")
    assert not session.diff(rebase=False).empty
    assert not session.diff(rebase=False).empty  # same diff again


def test_diff_with_rebase_advances_baseline(session, tmp_path):
    session.push_dir(_seed(tmp_path / "t", {}))
    session.shell("echo x > f.txt")
    assert not session.diff(rebase=True).empty
    assert session.diff().empty  # baseline advanced


def test_reset_discards_staged_writes(session, tmp_path):
    session.push_dir(_seed(tmp_path / "t", {"orig.txt": "original"}))
    session.shell("echo junk > junk.txt && echo clobber > orig.txt")
    session.reset()
    d = session.diff()
    assert d.empty
    r = session.shell("cat orig.txt && ls")
    assert "original" in r.transcript and "junk.txt" not in r.transcript


def test_python_writes_appear_in_diff(session, tmp_path):
    session.push_dir(_seed(tmp_path / "t", {}))
    session.python("open('made.txt', 'w').write('via python')")
    d = session.diff()
    assert d.writes.get("made.txt") == b"via python"


def test_fresh_push_resets_everything(session, tmp_path):
    session.push_dir(_seed(tmp_path / "a", {"one.txt": "1"}))
    session.shell("echo extra > extra.txt")
    session.push_dir(_seed(tmp_path / "b", {"two.txt": "2"}))
    r = session.shell("ls")
    assert "two.txt" in r.transcript
    assert "one.txt" not in r.transcript and "extra.txt" not in r.transcript
    assert session.diff().empty
