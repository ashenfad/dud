"""Layer flattening, whiteouts, and dud injection."""

from __future__ import annotations

from dud.images import rootfs
from dud.images.cpio import S_IFDIR


def test_later_layer_overrides_earlier(make_layer):
    l1 = make_layer("l1", files={"app/config": "v1"})
    l2 = make_layer("l2", files={"app/config": "v2"})
    fs = rootfs.flatten_layers([l1, l2])
    assert fs.nodes["app/config"].data == b"v2"


def test_whiteout_deletes_file(make_layer):
    l1 = make_layer("l1", files={"app/keep": "1", "app/drop": "2"})
    l2 = make_layer("l2", whiteouts=["app/.wh.drop"])
    fs = rootfs.flatten_layers([l1, l2])
    assert "app/keep" in fs.nodes
    assert "app/drop" not in fs.nodes


def test_opaque_whiteout_clears_directory(make_layer):
    l1 = make_layer("l1", files={"d/old1": "1", "d/old2": "2"})
    l2 = make_layer(
        "l2", whiteouts=["d/.wh..wh..opq"], files={"d/fresh": "3"},
    )
    fs = rootfs.flatten_layers([l1, l2])
    assert "d/old1" not in fs.nodes and "d/old2" not in fs.nodes
    assert fs.nodes["d/fresh"].data == b"3"


def test_symlink_preserved(make_layer):
    l1 = make_layer("l1", symlinks={"usr/bin/py": "python3.12"})
    fs = rootfs.flatten_layers([l1])
    assert fs.nodes["usr/bin/py"].data == b"python3.12"


def test_path_traversal_rejected(make_layer):
    l1 = make_layer("l1", files={"../escape": "x", "ok": "y"})
    fs = rootfs.flatten_layers([l1])
    assert "ok" in fs.nodes
    assert not any("escape" in n for n in fs.nodes)


def test_inject_dud_targets_site_packages(make_layer):
    l1 = make_layer("l1", dirs=["usr/local/lib/python3.12/site-packages"])
    fs = rootfs.flatten_layers([l1])
    site = rootfs.inject_dud(fs)
    assert site == "usr/local/lib/python3.12/site-packages"
    key = f"{site}/dud/guest/supervisor.py"
    assert key in fs.nodes and fs.nodes[key].data


def test_hardlink_same_layer_adopts_contents(make_layer):
    l1 = make_layer("l1", files={"a/orig": "data"},
                    hardlinks={"a/link": "a/orig"})
    fs = rootfs.flatten_layers([l1])
    assert fs.nodes["a/link"].data == b"data"


def test_hardlink_across_layers_adopts_lower_contents(make_layer):
    l1 = make_layer("l1", files={"a/orig": "lower-data"})
    l2 = make_layer("l2", hardlinks={"a/link": "a/orig"})
    fs = rootfs.flatten_layers([l1, l2])
    assert fs.nodes["a/link"].data == b"lower-data"


def test_hardlink_missing_target_fails_loudly(make_layer):
    import pytest

    l1 = make_layer("l1", hardlinks={"a/link": "not/there"})
    with pytest.raises(ValueError, match="target not found"):
        rootfs.flatten_layers([l1])


def test_writes_resolve_through_symlinked_parents(make_layer):
    """merged-usr shape: lib -> usr/lib, later layer writes lib/foo."""
    l1 = make_layer("l1", dirs=["usr/lib"], symlinks={"lib": "usr/lib"})
    l2 = make_layer("l2", files={"lib/foo/x": "hi"})
    fs = rootfs.flatten_layers([l1, l2])
    assert fs.nodes["usr/lib/foo/x"].data == b"hi"
    assert "lib/foo/x" not in fs.nodes
    assert fs.nodes["lib"].data == b"usr/lib"  # symlink untouched


def test_writes_resolve_through_absolute_symlink(make_layer):
    l1 = make_layer("l1", dirs=["usr/lib"], symlinks={"lib": "/usr/lib"})
    l2 = make_layer("l2", files={"lib/x": "hi"})
    fs = rootfs.flatten_layers([l1, l2])
    assert fs.nodes["usr/lib/x"].data == b"hi"


def test_dir_entry_does_not_clobber_symlink(make_layer):
    """A layer's ./lib dir entry keeps an existing lib -> usr/lib."""
    l1 = make_layer("l1", dirs=["usr/lib"], symlinks={"lib": "usr/lib"})
    l2 = make_layer("l2", dirs=["lib"], files={"lib/x": "hi"})
    fs = rootfs.flatten_layers([l1, l2])
    assert fs.nodes["lib"].data == b"usr/lib"  # still a symlink
    assert fs.nodes["usr/lib/x"].data == b"hi"


def test_symlink_loop_drops_entry(make_layer):
    l1 = make_layer("l1", symlinks={"a": "b", "b": "a"})
    l2 = make_layer("l2", files={"a/x": "hi"})
    fs = rootfs.flatten_layers([l1, l2])
    assert not any(k.endswith("/x") for k in fs.nodes)


def test_build_fileset_requires_interpreter(make_layer):
    import pytest

    from dud.images.registry import PulledImage, ImageRef

    l1 = make_layer("l1", dirs=["usr/local/lib/python3.12/site-packages"])
    img = PulledImage(
        ref=ImageRef.parse("python:3.12-slim"),
        digest="sha256:deadbeef", config={}, layer_paths=[l1],
    )
    with pytest.raises(ValueError, match="no /usr/local/bin/python3"):
        rootfs.build_fileset(img)


def test_build_fileset_adds_init_and_workspace(make_layer):
    l1 = make_layer(
        "l1",
        dirs=["usr/local/lib/python3.12/site-packages"],
        files={"usr/local/bin/python3": b"\x7fELF"},
    )
    from dud.images.registry import PulledImage, ImageRef

    img = PulledImage(
        ref=ImageRef.parse("python:3.12-slim"),
        digest="sha256:deadbeef", config={}, layer_paths=[l1],
    )
    fs = rootfs.build_fileset(img, workspace="/workspace")
    assert fs.nodes["workspace"].mode & S_IFDIR
    init = fs.nodes["init"]
    assert init.mode & 0o111  # executable
    body = init.data.decode()
    assert body.startswith("#!/usr/local/bin/python3")
    assert "from dud.guest.init import main" in body
    assert "default_root='/workspace'" in body

    # dud-emit on the guest's PATH. Worth pinning here rather than
    # leaving it to conformance: the subprocess rung gets the command
    # from the installed console script, so every shared test would
    # pass with the rootfs shipping nothing at all, and only a VM run
    # would notice.
    shim = fs.nodes["usr/local/bin/dud-emit"]
    assert shim.mode & 0o111
    assert shim.data.startswith(b"#!/usr/local/bin/python3")
    assert b"from dud.guest.emit import main" in shim.data


def test_the_emit_module_is_injected_for_its_shim(make_layer):
    """The shim is two lines onto `dud.guest.emit`, so shipping one
    without the other is a guest that boots with a command that cannot
    import itself."""
    l1 = make_layer("l1", dirs=["usr/local/lib/python3.12/site-packages"])
    fs = rootfs.flatten_layers([l1])
    site = rootfs.inject_dud(fs)
    assert f"{site}/dud/guest/emit.py" in fs.nodes


# ---- image ENV into /init ----------------------------------------------


def test_image_env_parses_docker_style_entries():
    from dud.images.rootfs import _image_env

    got = _image_env([
        "PATH=/usr/local/bin:/usr/bin",
        "LANG=C.UTF-8",
        "EMPTY=",
        "malformed-no-equals",
        "=novalue",
    ])
    assert got == {
        "PATH": "/usr/local/bin:/usr/bin",
        "LANG": "C.UTF-8",
        "EMPTY": "",
    }


def test_init_script_applies_env_before_handing_off():
    """Order matters twice over: the ENV has to land before main() runs,
    because main() prepends dud's site dir to PYTHONPATH and the
    supervisor snapshots the environment as _boot_env right after."""
    from dud.images.rootfs import _init_script

    src = _init_script("usr/local/lib/python3.12/site-packages", "/workspace",
                       {"PATH": "/usr/local/bin", "LANG": "C.UTF-8"})
    text = src.decode()
    assert "os.environ.update" in text
    assert "/usr/local/bin" in text and "C.UTF-8" in text
    assert text.index("os.environ.update") < text.index("main(default_root=")


def test_image_env_overrides_the_kernel_presets():
    """The kernel hands PID 1 two hardcoded constants, HOME=/ and
    TERM=linux. Deferring to them would silently discard an image that
    declares either — and ENV HOME=/app is ordinary for app images."""
    import subprocess
    import sys as _sys

    from dud.images.rootfs import _init_script

    src = _init_script("site", "/workspace", {"HOME": "/app"}).decode()
    # Everything above the hand-off to main(), run with the kernel's
    # value already in place.
    prelude = src.split("from dud.guest.init import main")[0]
    prelude = prelude.split("\n", 1)[1]  # drop the shebang
    out = subprocess.run(
        [_sys.executable, "-c", prelude + "\nprint(os.environ['HOME'])"],
        capture_output=True, text=True, env={"HOME": "/", "TERM": "linux"},
    )
    assert out.stdout.strip() == "/app", out


def test_init_script_without_env_still_boots():
    from dud.images.rootfs import _init_script

    text = _init_script("site", "/workspace").decode()
    assert "main(default_root='/workspace')" in text


# ---- baked bytecode ----------------------------------------------------


def test_bake_pyc_covers_the_stdlib_and_the_injected_runtime(make_layer):
    """The gap this closes: python:*-slim deletes every .pyc at image
    build, and dud's own runtime is injected as source — so a guest
    compiled ~1100 stdlib modules on the way up, and on a read-only
    erofs root could never cache the result.

    Verified against a real cached rootfs before the fix: 1116 stdlib
    .py, 0 .pyc; 19 dud .py, 0 .pyc.
    """
    import sys

    tag = sys.implementation.cache_tag
    host = f"{sys.version_info.major}.{sys.version_info.minor}"
    site = f"usr/local/lib/python{host}/site-packages"
    l1 = make_layer("l1", dirs=[site],
                    files={f"usr/local/lib/python{host}/json/tool.py": b"x = 1\n"})
    fs = rootfs.flatten_layers([l1])
    rootfs.inject_dud(fs)
    assert rootfs.bake_pyc(fs, site) > 0

    stdlib = f"usr/local/lib/python{host}/json/__pycache__/tool.{tag}.pyc"
    assert stdlib in fs.nodes
    assert fs.nodes[stdlib].data[:4] == __import__("importlib.util",
                                                   fromlist=["util"]).MAGIC_NUMBER


def test_bake_pyc_skips_a_version_mismatch_rather_than_shipping_junk(make_layer):
    """Bytecode is minor-version scoped. A mismatch has to degrade to
    slower imports, never to a rootfs full of .pyc the guest will
    refuse — same rule the layered wheels already followed."""
    site = "usr/local/lib/python2.7/site-packages"
    l1 = make_layer("l1", dirs=[site],
                    files={"usr/local/lib/python2.7/json/tool.py": b"x = 1\n"})
    fs = rootfs.flatten_layers([l1])
    assert rootfs.bake_pyc(fs, site) == 0
    assert not any("__pycache__" in k for k in fs.nodes)


def test_bake_pyc_survives_an_unparseable_module(make_layer):
    """The stdlib carries test fixtures that are deliberately invalid.
    One of them must not fail an image build."""
    import sys

    host = f"{sys.version_info.major}.{sys.version_info.minor}"
    site = f"usr/local/lib/python{host}/site-packages"
    l1 = make_layer("l1", dirs=[site], files={
        f"usr/local/lib/python{host}/lib2to3/bad.py": b"this is not python(",
        f"usr/local/lib/python{host}/json/ok.py": b"x = 1\n",
    })
    fs = rootfs.flatten_layers([l1])
    assert rootfs.bake_pyc(fs, site) >= 1  # the good one still baked


def test_bake_pyc_skips_a_symlinked_module(make_layer):
    """`Node.data` holds a symlink's TARGET, not its contents.

    So `foo.py -> bar.py` would compile the string "bar.py" — perfectly
    valid source for the expression `bar.py` — into a .pyc named for
    foo. And because these are written UNCHECKED_HASH, importing foo
    would run that without ever consulting the real module, raising
    NameError instead of executing bar.
    """
    import sys

    from dud.images.cpio import Node, S_IFLNK

    host = f"{sys.version_info.major}.{sys.version_info.minor}"
    tag = sys.implementation.cache_tag
    lib = f"usr/local/lib/python{host}"
    site = f"{lib}/site-packages"
    l1 = make_layer("l1", dirs=[site],
                    files={f"{lib}/bar.py": b"value = 1\n"})
    fs = rootfs.flatten_layers([l1])
    fs.nodes[f"{lib}/foo.py"] = Node(mode=S_IFLNK | 0o777, data=b"bar.py")

    rootfs.bake_pyc(fs, site)
    assert f"{lib}/__pycache__/bar.{tag}.pyc" in fs.nodes  # the real one
    assert f"{lib}/__pycache__/foo.{tag}.pyc" not in fs.nodes


def test_bake_pyc_ignores_a_directory_named_like_a_module(make_layer):
    """Same filter, other shape: `.py` in a name proves nothing."""
    import sys

    host = f"{sys.version_info.major}.{sys.version_info.minor}"
    tag = sys.implementation.cache_tag
    lib = f"usr/local/lib/python{host}"
    site = f"{lib}/site-packages"
    l1 = make_layer("l1", dirs=[site, f"{lib}/weird.py"])
    fs = rootfs.flatten_layers([l1])
    rootfs.bake_pyc(fs, site)
    assert f"{lib}/__pycache__/weird.{tag}.pyc" not in fs.nodes


def test_bake_pyc_ignores_the_builders_own_optimization_level():
    """Bytecode written as `module.<tag>.pyc` is what a normally started
    guest loads, and that name promises asserts intact and `__debug__`
    true. `dont_inherit` does not cover this — it governs __future__
    flags, while `optimize` defaults to -1, meaning "whatever this
    interpreter was started with". Building under `python -O` would
    therefore bake assert-stripped bytecode under the un-optimized
    name and silently change the guest's semantics.

    Run in a real `-O` subprocess, because sys.flags.optimize cannot be
    moved from inside the process it describes.
    """
    import subprocess
    import sys
    import textwrap

    prog = textwrap.dedent("""
        import sys
        from dud.images import rootfs
        from dud.images.cpio import FileSet

        host = f"{sys.version_info.major}.{sys.version_info.minor}"
        site = f"usr/local/lib/python{host}/site-packages"
        fs = FileSet()
        fs.add_file(f"{site}/m.py", b"d = __debug__\\n", 0o644)
        assert sys.flags.optimize > 0, "the -O flag did not take"
        rootfs.bake_pyc(fs, site)

        tag = sys.implementation.cache_tag
        pyc = fs.nodes[f"{site}/__pycache__/m.{tag}.pyc"].data
        import marshal
        code = marshal.loads(pyc[16:])
        # __debug__ folds at compile time: True at optimize=0, False
        # under -O. Identity, not equality -- False == 0 would match a
        # stray integer constant.
        print("DEBUG_TRUE" if any(c is True for c in code.co_consts)
              else "DEBUG_FALSE")
    """)
    out = subprocess.run([sys.executable, "-O", "-c", prog],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "DEBUG_TRUE", out.stdout
