"""Conformance: push_tree / pull_diff / reset — the state seam."""

import os

import pytest

_BACKEND = os.environ.get("DUD_BACKEND", "subprocess")


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


def test_staging_matches_backend(session):
    """No silent fallback: the VM rung must actually run overlay."""
    expected = {"subprocess": "scan", "vfkit": "overlay",
                "firecracker": "overlay"}[_BACKEND]
    assert session.ping().get("staging") == expected


def test_directory_delete_reports_per_file_deletes(session, tmp_path):
    session.push_dir(_seed(tmp_path / "t", {
        "d/a.txt": "a", "d/sub/b.txt": "b", "top.txt": "t",
    }))
    session.shell("rm -rf d")
    d = session.diff()
    assert not d.writes
    assert sorted(d.deletes) == ["d/a.txt", "d/sub/b.txt"]


def test_create_then_delete_is_a_noop_diff(session, tmp_path):
    session.push_dir(_seed(tmp_path / "t", {"base.txt": "b"}))
    session.shell("echo tmp > scratch.txt && rm scratch.txt")
    assert session.diff().empty


def test_identical_rewrite_is_not_a_write(session, tmp_path):
    session.push_dir(_seed(tmp_path / "t", {"same.txt": "stable\n"}))
    session.shell("printf 'stable\\n' > same.txt && touch same.txt")
    assert session.diff().empty


def test_dir_replaced_by_file_and_back(session, tmp_path):
    session.push_dir(_seed(tmp_path / "t", {"thing/inner.txt": "i"}))
    session.shell("rm -rf thing && echo flat > thing")
    d = session.diff(rebase=True)
    assert d.writes.get("thing") == b"flat\n"
    assert d.deletes == ["thing/inner.txt"]
    session.shell("rm thing && mkdir thing && echo back > thing/inner.txt")
    d2 = session.diff()
    assert d2.writes.get("thing/inner.txt") == b"back\n"
    assert d2.deletes == ["thing"]


def test_symlink_shadowing_file_reports_delete(session, tmp_path):
    """A symlink covering a pushed file hides it from the merged index:
    both producers must report the delete (and no write — symlinks
    don't round-trip in v0)."""
    session.push_dir(_seed(tmp_path / "t", {
        "data.txt": "d", "target.txt": "t",
    }))
    session.shell("ln -sf target.txt data.txt")
    d = session.diff()
    assert not d.writes
    assert d.deletes == ["data.txt"]


@pytest.mark.skipif(
    _BACKEND == "subprocess",
    reason="rung-1 documented gap: no fs isolation to enforce read-only",
)
def test_fs_readonly_exec_blocks_writes(session, tmp_path):
    session.push_dir(_seed(tmp_path / "t", {"data.txt": "d"}))
    r = session.python(
        "try:\n"
        "    open('evil.txt', 'w').write('x')\n"
        "    blocked = False\n"
        "except OSError:\n"
        "    blocked = True\n"
        "readable = open('data.txt').read()",
        fs_readonly=True,
    )
    assert r.ok and r.outputs["blocked"] is True
    assert r.outputs["readable"] == "d"
    assert session.diff().empty
    # The window closes with the exec: normal writes work again.
    r2 = session.python("open('after.txt', 'w').write('fine')")
    assert r2.ok
    assert session.diff().writes.get("after.txt") == b"fine"


# ---- permission bits ---------------------------------------------------


def test_executable_bit_survives_the_diff(session):
    """The round trip used to be quietly destructive: an agent runs
    chmod +x, the checkpoint stores bytes, and next session the script
    isn't executable. The mode crossed the wire the whole time — the
    host decode just dropped it."""
    session.shell("printf '#!/bin/sh\\necho hi\\n' > run.sh && chmod +x run.sh")
    d = session.diff()
    assert "run.sh" in d.writes
    assert d.modes["run.sh"] == 0o755
    assert d.mode("run.sh") == 0o755


def test_ordinary_files_carry_no_mode(session):
    """Only departures from the plain-file default are recorded, so the
    map stays empty in the common case and a consumer that ignores it
    behaves exactly as it did before."""
    session.shell("echo plain > notes.txt")
    d = session.diff()
    assert "notes.txt" in d.writes
    assert "notes.txt" not in d.modes
    assert d.mode("notes.txt") == 0o644


def test_setuid_is_not_carried_across_the_boundary(session):
    """Permission bits only. A diff is not a place to smuggle setuid
    into whatever restores it."""
    session.shell("echo x > sneaky && chmod 4755 sneaky")
    d = session.diff()
    assert d.modes["sneaky"] == 0o755  # the setuid bit is gone
    assert d.modes["sneaky"] & 0o7000 == 0


def test_diff_keeps_the_raw_archive(session):
    """Lossless escape hatch: symlinks and xattrs aren't in this shape
    yet, and a consumer needing one shouldn't wait for a new field."""
    import io
    import tarfile

    session.shell("echo hi > a.txt && chmod +x a.txt")
    d = session.diff()
    assert d.tar
    with tarfile.open(fileobj=io.BytesIO(d.tar), mode="r:*") as tf:
        member = tf.getmember("a.txt")
    assert member.mode & 0o777 == 0o755


def test_mode_round_trips_back_into_a_session(session, tmp_path):
    """End to end: what comes out of a diff can be put back and still
    run. This is the property the checkpoint boundary needs."""
    session.shell("printf '#!/bin/sh\\necho ran\\n' > tool.sh && chmod +x tool.sh")
    d = session.diff()

    # Push the archive straight back, the way a provider that kept it
    # would — the same bytes, so nothing had a chance to be lost.
    session.push_tree(d.tar)
    r = session.shell("./tool.sh")
    assert "ran" in r.transcript, r.transcript
