"""Conformance: exec_python / runner semantics every rung must satisfy."""


def test_stdout_transcript(session):
    r = session.python("print('hi', 42)")
    assert r.ok and r.transcript == "hi 42\n"


def test_outputs_harvest(session):
    r = session.python("x = 41 + 1\nwords = ['a', 'b']\n_private = 'hidden'")
    assert r.outputs == {"x": 42, "words": ["a", "b"]}


def test_outputs_skipped_records_type(session):
    r = session.python("x = 1\nbad = object()")
    assert r.outputs == {"x": 1}
    assert r.outputs_skipped == {"bad": "object"}


def test_bytes_output(session):
    r = session.python("blob = b'\\x00\\x01'")
    assert r.outputs["blob"] == b"\x00\x01"


def test_inputs_bound(session):
    r = session.python("y = n * 2", inputs={"n": 21})
    assert r.outputs["y"] == 42


def test_last_expression_echo(session):
    r = session.python("x = 40\nx + 2")
    assert "42" in r.transcript
    echoes = [p for p in r.prints if p.get("echo")]
    assert len(echoes) == 1 and echoes[0]["text"] == "42"


def test_no_echo_for_none_or_statement(session):
    r = session.python("x = 1")
    assert not [p for p in r.prints if p.get("echo")]


def test_prints_structured(session):
    r = session.python("print([1, 2, 3])")
    assert r.prints[0]["type"] == "list"
    assert r.prints[0]["len"] == 3
    assert r.prints[0]["text"] == "[1, 2, 3]"


def test_prints_entry_cap(session):
    r = session.python("print('x' * 10000)", caps={"entry": 100})
    assert r.prints[0]["truncated"] and len(r.prints[0]["text"]) == 100
    # transcript keeps the full text (its own cap governs it)
    assert len(r.transcript) > 100


def test_error_reports_traceback(session):
    r = session.python("def f():\n    raise ValueError('boom')\nf()")
    assert not r.ok
    assert r.error.etype == "ValueError" and "boom" in r.error.message
    assert "<session>" in r.error.traceback


def test_syntax_error(session):
    r = session.python("def broken(:")
    assert not r.ok and r.error.etype == "SyntaxError"


def test_timeout_kills_runner(session):
    r = session.python("import time\ntime.sleep(30)", timeout=1.0)
    assert not r.ok and r.error.etype == "Timeout"


def test_session_survives_runner_timeout(session):
    session.python("import time\ntime.sleep(30)", timeout=1.0)
    r = session.python("x = 1")
    assert r.ok and r.outputs == {"x": 1}


def test_files_shared_between_shell_and_python(session):
    session.shell("echo 'a,b\\n1,2' > data.csv")
    r = session.python("rows = open('data.csv').read().count(',')")
    assert r.ok and r.outputs["rows"] == 2
    r2 = session.python("open('out.txt', 'w').write('from python')")
    assert r2.ok
    r3 = session.shell("cat out.txt")
    assert r3.transcript == "from python"


def test_python_cwd_follows_shell(session):
    session.shell("mkdir -p deep && cd deep")
    session.python("open('here.txt', 'w').write('x')")
    r = session.shell("ls")
    assert "here.txt" in r.transcript
