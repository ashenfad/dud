"""Conformance: the guest runs the image's environment (VM rungs).

DESIGN's claim is that the image spec IS the config surface — layer
what the agent needs and the image is the allowlist. That only holds if
the guest actually boots with the environment its image declares.

VM-rung only: rung 1 is a host process in a temp dir, with no image to
take an environment from.
"""

import os

import pytest

_BACKEND = os.environ.get("DUD_BACKEND", "subprocess")

pytestmark = pytest.mark.skipif(
    _BACKEND not in ("vfkit", "firecracker"),
    reason="no image on the subprocess rung, so no image ENV to apply",
)


def test_path_comes_from_the_image(session):
    """Without this the kernel hands init almost nothing and there is no
    PATH at all — which bash hides behind its own fallback while every
    other consumer fails to find /usr/local/bin."""
    r = session.python("import os\npath = os.environ.get('PATH', '')")
    assert r.ok, r.error
    assert "/usr/local/bin" in r.outputs["path"]


def test_agent_code_can_run_image_tools(session):
    """/usr/local/bin is where the image keeps python, pip and every
    console script — including anything packages=[...] layers in. Agent
    code reaching them is the whole point of layering them."""
    r = session.python(
        "import subprocess\n"
        "rc = subprocess.run(['python3', '-c', 'print(1)'],\n"
        "                    capture_output=True, timeout=20).returncode"
    )
    assert r.ok, r.error
    assert r.outputs["rc"] == 0


def test_python_launched_from_bash_knows_its_own_path(session):
    """sys.executable was the empty string: CPython resolves argv[0]
    against a PATH that wasn't there. Anything re-invoking the
    interpreter — venv, self-spawning tools — died on it."""
    r = session.shell("python3 -c 'import sys; print(repr(sys.executable))'")
    assert "/usr/local/bin/python" in r.transcript, r.transcript


def test_dud_pythonpath_still_wins(session):
    """The image may ship its own PYTHONPATH. dud's injected site dir
    has to stay ahead of it, or the guest runtime stops resolving."""
    r = session.python("import os\npp = os.environ.get('PYTHONPATH', '')")
    assert r.ok, r.error
    assert r.outputs["pp"].split(os.pathsep)[0].endswith("site-packages")


def test_workdir_is_the_workspace_not_the_image(session):
    """Env yes, WORKDIR no. The image records '/' and dud's contract is
    that execs start at the workspace root — that one is dud's call."""
    r = session.shell("pwd")
    assert r.transcript.strip() == "/workspace", r.transcript
