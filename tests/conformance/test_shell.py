"""Conformance: exec_shell semantics every rung must satisfy."""


def test_basic_echo(session):
    r = session.shell("echo hello")
    assert r.ok and r.transcript == "hello\n"


def test_cwd_persists_across_calls(session):
    session.shell("mkdir -p sub/dir && cd sub/dir")
    r = session.shell("pwd")
    assert r.transcript.strip().endswith("sub/dir")


def test_env_persists_across_calls(session):
    session.shell("export DUD_TEST_VAR=abc123")
    r = session.shell("echo $DUD_TEST_VAR")
    assert r.transcript.strip() == "abc123"


def test_cwd_persists_even_on_failure(session):
    session.shell("mkdir -p d && cd d && false")
    r = session.shell("pwd")
    assert r.transcript.strip().endswith("/d")


def test_exit_codes(session):
    assert session.shell("true").exit_code == 0
    assert session.shell("false").exit_code == 1
    assert session.shell("exit 7").exit_code == 7


def test_stderr_in_transcript(session):
    r = session.shell("echo out; echo err >&2")
    assert "out" in r.transcript and "err" in r.transcript


def test_pipes_and_real_tools(session):
    r = session.shell("printf 'b\\na\\nc\\n' | sort | head -2 | tr a-z A-Z")
    assert r.transcript == "A\nB\n"


def test_timeout_kills(session):
    r = session.shell("sleep 30", timeout=1.0)
    assert r.timed_out and r.exit_code == 124


def test_workspace_env_var(session):
    r = session.shell("test -d \"$DUD_WORKSPACE\" && echo yes")
    assert r.transcript.strip() == "yes"


def test_medium_env_vars_persist_across_calls(session):
    """The env snapshot uses bash builtins, so it survives values the
    external `env` binary choked on (its execve carried the whole
    environment; one biggish var broke the ENTIRE snapshot)."""
    session.shell("export MEDIUM=$(python3 -c 'print(\"m\" * 60000)') "
                  "&& export MULTI=$'a\\nb=c'")
    r = session.shell("echo len=${#MEDIUM}")
    assert "len=60000" in r.transcript
    r2 = session.python("import os\nmulti = os.environ.get('MULTI')")
    assert r2.ok and r2.outputs["multi"] == "a\nb=c"


def test_oversized_env_var_drops_alone(session):
    """A single var past Linux's per-string execve cap (128 KB) cannot
    cross later spawns; it must drop ALONE — not poison the snapshot
    (everything else keeps persisting) and not break the session.
    Uniform on every rung; big data belongs in files, not env."""
    r0 = session.shell("export KEEP=kept "
                       "&& export HUGE=$(python3 -c 'print(\"x\" * 300000)') "
                       "&& echo len=${#HUGE}")
    assert "len=300000" in r0.transcript  # visible within its own call
    r = session.python("import os\n"
                       "keep = os.environ.get('KEEP')\n"
                       "huge = len(os.environ.get('HUGE', ''))")
    assert r.ok and r.outputs["keep"] == "kept" and r.outputs["huge"] == 0
    assert session.shell("echo still-works").transcript.strip() == "still-works"
