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


def test_default_caps_do_not_shape_an_ordinary_observation(session):
    """The guards sit above anything a caller would plausibly want to
    show a model, so the host — not dud — decides what gets trimmed.
    10k characters used to be truncated twice over by the old 2 KB
    entry / 20 KB transcript defaults."""
    r = session.python("print('x' * 10000)")
    assert r.ok, r.error
    assert not r.prints[0]["truncated"]
    assert len(r.prints[0]["text"]) == 10000
    assert "truncated at" not in r.transcript


def test_total_cap_bounds_the_entry_stream(session):
    """entry * entries multiplies, so a total is what actually guards
    the supervisor's memory. Dropped entries are counted, not silent."""
    r = session.python(
        "for _ in range(200):\n    print('y' * 1000)\n",
        caps={"total": 5_000, "entry": 1_000, "entries": 500},
    )
    assert r.ok, r.error
    assert sum(len(p["text"]) for p in r.prints) <= 5_000  # a hard bound
    assert r.prints_dropped > 0


def test_total_cap_bounds_a_single_oversized_entry(session):
    """Checked against the remaining budget, not the running total —
    otherwise the first entry sails past any total in full, and a guard
    you can't lower to bound an untrusted exec isn't a guard."""
    r = session.python("print('x' * 10000)", caps={"total": 500})
    assert r.ok, r.error
    assert len(r.prints[0]["text"]) == 500
    assert r.prints[0]["truncated"]
    # ...and the transcript is still governed only by its own cap
    assert len(r.transcript) > 500


def test_caps_can_still_be_tightened_per_exec(session):
    """Guards are a floor the caller can lower for an untrusted exec,
    even though they are not an observation budget."""
    r = session.python("print('z' * 5000)", caps={"stdout": 100})
    assert r.ok, r.error
    assert "truncated at 100 chars" in r.transcript


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


def test_timeout_keeps_what_was_printed(session):
    """A hang is when the transcript is worth the most: it says how far
    the code got. It used to come back empty, so the only way to learn
    anything was to re-run with more prints, blind."""
    r = session.python(
        "print('reached step 1')\nimport time\ntime.sleep(30)", timeout=1.0
    )
    assert not r.ok and r.error.etype == "Timeout"
    assert "reached step 1" in r.transcript


def test_crash_keeps_what_was_printed(session):
    """Same for a runner that dies without answering — the failure mode
    a real machine invites, since C extensions segfault and allocations
    get OOM-killed."""
    r = session.python("print('reached step 2')\nimport os\nos._exit(1)")
    assert not r.ok and r.error.etype == "RunnerCrash"
    assert "reached step 2" in r.transcript


def test_chatty_hang_does_not_deadlock(session):
    """The output escapes through a pipe, and an undrained pipe blocks
    its writer at 64 KB. Printing well past that and then hanging must
    still time out on schedule, keeping the tail."""
    r = session.python(
        "for i in range(20000):\n"
        "    print(f'line {i} ' + 'y' * 40)\n"
        "import time\n"
        "time.sleep(30)\n",
        timeout=3.0,
    )
    assert not r.ok and r.error.etype == "Timeout"
    assert r.transcript.endswith("y\n")  # the tail, not the head
    assert "earlier output dropped" in r.transcript


def test_successful_exec_transcript_is_not_duplicated(session):
    """The mirror the failure paths read is discarded on success — the
    response carries the authoritative, capped transcript."""
    r = session.python("print('once')")
    assert r.ok, r.error
    assert r.transcript == "once\n"


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


def test_root_imports_survive_cwd_changes(session):
    """Filesystem modules resolve from the workspace ROOT regardless of
    the shell's cwd (the VFS executors' documented contract). Regression:
    a session whose agent `cd app`'d broke every `from app... import` in
    later execs and app handlers."""
    session.shell(
        "mkdir -p pkg/sub && echo 'X = 41' > rootmod.py && "
        "printf 'from rootmod import X\\nY = X + 1\\n' > pkg/mod.py && cd pkg/sub"
    )
    r = session.python("import rootmod\nfrom pkg import mod\nv = mod.Y")
    assert r.ok, r.error
    assert r.outputs["v"] == 42


# ---- print rendering ---------------------------------------------------


def test_ping_reports_the_live_renderer(session):
    """Rendering falls back silently when the image has no reprobate, so
    the fallback has to be observable — same reason ping reports which
    staging strategy is live."""
    assert session.ping()["renderer"] in ("reprobate", "plain")


def test_ping_reports_the_outputs_hook(session):
    """Same reason as the renderer: rich `ui` flattening falls back to
    nothing when the image ships no dud_outputs, and a consumer needs
    to see that rather than wonder where its charts went."""
    assert session.ping()["outputs_hook"] in ("dud_outputs", "none")


def test_rich_ui_without_a_hook_is_reported_not_invented(session):
    """dud's zero-knowledge default. Without an image hook, a value
    that cannot cross the codec is named in outputs_skipped with its
    type — never guessed at, never written somewhere the consumer did
    not ask for."""
    r = session.python("class Fig:\n    pass\nui = {'chart': Fig()}")
    assert r.ok, r.error
    assert "ui" in r.outputs_skipped
    ls = session.shell("ls ui 2>/dev/null || true")
    assert ls.transcript.strip() == ""


def test_no_render_budget_means_plain_text(session):
    """dud never invents an observation size. Unasked, entries carry
    exactly what print produced."""
    r = session.python("print(list(range(100)))")
    assert r.ok, r.error
    assert r.prints[0]["text"] == str(list(range(100)))
    assert not r.prints[0].get("elided")


def test_render_budget_elides_structurally(session):
    """The gap this closes: the host used to get a mid-token head-cut
    where the in-process executor got structural elision. Rendering
    needs the live object, so it can only happen here."""
    if session.ping()["renderer"] != "reprobate":
        import pytest

        pytest.skip("image has no reprobate; see the fallback test")
    r = session.python("print(list(range(100)))", render_budget=60)
    assert r.ok, r.error
    entry = r.prints[0]
    assert entry["elided"] is True
    assert "more" in entry["text"]  # "...86 more", not a severed token
    assert len(entry["text"]) < 200


def test_render_budget_falls_back_without_reprobate(session):
    """Degrades to plain text rather than failing the exec — rendering
    is an optimization on the observation, never a correctness input."""
    if session.ping()["renderer"] != "plain":
        import pytest

        pytest.skip("image has reprobate; see the elision test")
    r = session.python("print(list(range(100)))", render_budget=60)
    assert r.ok, r.error
    assert not r.prints[0].get("elided")


def test_transcript_is_never_rendered(session):
    """Fidelity outranks the observation: print(x) must produce what a
    real machine produces. Only the structured entry is summarized."""
    r = session.python("print(list(range(100)))", render_budget=60)
    assert r.ok, r.error
    assert r.transcript == str(list(range(100))) + "\n"


def test_render_budget_applies_to_the_echo(session):
    if session.ping()["renderer"] != "reprobate":
        import pytest

        pytest.skip("image has no reprobate")
    r = session.python("list(range(100))", render_budget=60)
    assert r.ok, r.error
    echo = [p for p in r.prints if p.get("echo")][0]
    assert echo["elided"] is True


def test_workspace_module_cannot_break_rendering(session):
    """The renderer resolves from the IMAGE. cwd is the workspace and
    `python -m` puts cwd on sys.path, so a stray reprobate.py beside the
    agent's own code used to shadow the real package — and a shadow
    without `render` raised AttributeError out of print(), failing an
    exec whose only crime was printing."""
    session.shell("echo 'x = 1' > reprobate.py")
    try:
        r = session.python("print('hello')", render_budget=60)
        assert r.ok, r.error
        assert r.transcript == "hello\n"
    finally:
        session.shell("rm -f reprobate.py")


def test_render_budget_holds_across_many_arguments(session):
    """A per-argument floor multiplies by the argument count: this used
    to run ~5x past the caller's number and then meet the entry guard as
    a mid-token cut, which is what rendering exists to avoid."""
    if session.ping()["renderer"] != "reprobate":
        import pytest

        pytest.skip("image has no reprobate")
    r = session.python("print(*range(100))", render_budget=60)
    assert r.ok, r.error
    text = r.prints[0]["text"]
    assert len(text) < 60 * 2  # bounded by the budget, not by arg count
    assert "more" in text  # the remainder is counted, not silently cut
