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
