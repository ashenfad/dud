"""The newc cpio writer, checked against an independent reader."""

from __future__ import annotations

from dud.images.cpio import (
    S_IFDIR,
    S_IFLNK,
    S_IFREG,
    FileSet,
    build_cpio,
    build_cpio_gz,
)


def parse_newc(buf: bytes) -> list[tuple[str, int, int, int, bytes]]:
    """Minimal newc reader -> [(name, mode, uid, gid, data)] in file order."""
    out = []
    off = 0
    while True:
        assert buf[off:off + 6] == b"070701"
        f = [int(buf[off + 6 + i * 8: off + 6 + i * 8 + 8], 16) for i in range(13)]
        mode, uid, gid, fsize, nsize = f[1], f[2], f[3], f[6], f[11]
        ns = off + 110
        name = buf[ns:ns + nsize - 1].decode()
        ds = (ns + nsize + 3) & ~3
        data = buf[ds:ds + fsize]
        off = (ds + fsize + 3) & ~3
        if name == "TRAILER!!!":
            return out
        out.append((name, mode, uid, gid, data))


def test_roundtrip_files_dirs_symlinks():
    fs = FileSet()
    fs.add_dir("etc")
    fs.add_file("etc/hosts", b"127.0.0.1 localhost")
    fs.add_symlink("etc/localtime", "/usr/share/zoneinfo/UTC")
    entries = {n: (m, u, g, d) for n, m, u, g, d in parse_newc(build_cpio(fs))}

    assert entries["etc"][0] & S_IFDIR
    assert entries["etc/hosts"][0] & S_IFREG
    assert entries["etc/hosts"][3] == b"127.0.0.1 localhost"
    assert entries["etc/localtime"][0] & S_IFLNK
    assert entries["etc/localtime"][3] == b"/usr/share/zoneinfo/UTC"


def test_everything_is_root_owned():
    fs = FileSet()
    fs.add_file("a/b/c.txt", b"x")
    for _, _, uid, gid, _ in parse_newc(build_cpio(fs)):
        assert uid == 0 and gid == 0


def test_parents_precede_children():
    fs = FileSet()
    fs.add_file("a/b/c/deep.txt", b"x")
    names = [n for n, *_ in parse_newc(build_cpio(fs))]
    assert names.index("a") < names.index("a/b") < names.index("a/b/c")
    assert names.index("a/b/c") < names.index("a/b/c/deep.txt")


def test_ensure_parents_synthesizes_missing_dirs():
    fs = FileSet()
    fs.add_file("usr/local/bin/python", b"#!")
    names = {n for n, *_ in parse_newc(build_cpio(fs))}
    assert {"usr", "usr/local", "usr/local/bin"} <= names


def test_remove_subtree_prunes_descendants():
    fs = FileSet()
    fs.add_file("data/keep.txt", b"1")
    fs.add_file("data/sub/gone.txt", b"2")
    fs.remove_subtree("data/sub")
    names = {n for n, *_ in parse_newc(build_cpio(fs))}
    assert "data/keep.txt" in names
    assert "data/sub/gone.txt" not in names


def test_gz_is_deterministic():
    fs = FileSet()
    fs.add_file("a.txt", b"hello")
    assert build_cpio_gz(fs) == build_cpio_gz(fs)
