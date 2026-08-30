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


def test_ping_shows_a_render_hook_that_did_not_resolve(make_session):
    """A named renderer that failed to import still renders, because
    the chain continues — so without this report the caller sees
    reasonable output and never learns their hook was never used."""
    s = make_session(render_hook="nope_missing.mod:render")
    status = s.ping()["renderer"]
    assert status.startswith("nope_missing.mod:render (not found; using ")


def test_ping_reports_the_live_renderer(session):
    """Rendering falls back silently when the image has no reprobate, so
    the fallback has to be observable — same reason ping reports which
    staging strategy is live."""
    assert session.ping()["renderer"] in ("reprobate", "plain")


def test_ping_reports_no_hook_when_none_is_named(session):
    assert session.ping()["outputs_hook"] is None


def test_ping_shows_a_hook_that_did_not_resolve(make_session):
    """The case that needs reporting: an exec whose hook failed to
    import behaves exactly like one with no hook, so a typo in a
    package name is otherwise invisible."""
    s = make_session(outputs_hook="nope_missing.mod:flatten")
    assert s.ping()["outputs_hook"] == "nope_missing.mod:flatten (not found)"


def test_ping_names_a_malformed_hook_spec(make_session):
    """A bare dotted path is ambiguous about where the module ends, so
    it is refused rather than guessed at — and said so."""
    s = make_session(outputs_hook="pkg.mod.flatten")
    assert "not a 'pkg.module:function' spec" in s.ping()["outputs_hook"]


def test_rich_values_without_a_hook_are_reported_not_invented(session):
    """dud's zero-knowledge default. Without an image hook, a value
    that cannot cross the codec is named in outputs_skipped with its
    type — never guessed at, never written somewhere the consumer did
    not ask for. Note the binding is not called `ui`: dud names no
    binding, so the default holds whatever the caller called it."""
    r = session.python("class Fig:\n    pass\nchart = Fig()")
    assert r.ok, r.error
    assert r.outputs_skipped.get("chart") == "Fig"
    ls = session.shell("ls 2>/dev/null || true")
    assert "ui" not in ls.transcript


def test_an_oversized_binding_is_skipped_not_shipped(session):
    """The value guard, at the session level.

    An uncapped harvest let one assignment size the supervisor's
    memory — and on a VM rung the supervisor is PID 1 with the
    machine's whole RAM, so the failure is a panic rather than a bad
    exec. Skipped rather than truncated: half a value is a wrong
    answer, not a smaller one, and the name still comes back saying
    what happened.
    """
    r = session.python("data = 'z' * 20_000_000\nkeep = 7")
    assert r.ok, r.error
    assert r.outputs == {"keep": 7}  # the small one is untouched
    assert "data" in r.outputs_skipped
    assert "str" in r.outputs_skipped["data"]


def test_the_value_guard_is_the_callers_to_raise(session):
    """A guard, not a policy: a caller who wants a big value can have
    one. Both ceilings have to move — the per-value one and the total —
    and the message a skip carries names whichever is in the way."""
    r = session.python(
        "data = 'z' * 20_000_000",
        caps={"value": 64_000_000, "outputs": 64_000_000},
    )
    assert r.ok, r.error
    assert len(r.outputs["data"]) == 20_000_000
    assert r.outputs_skipped == {}


def test_a_binding_name_cannot_smuggle_a_payload(session):
    """The guard has to bound the FRAME, not just the values in it.

    `globals()['k' * N] = 1` charged one byte to the harvest total and
    put the whole name in the body the supervisor parses. The name is
    counted now — and the skip is filed under a truncated key, because
    reporting it verbatim would put the payload on the wire anyway.
    """
    r = session.python("globals()['k' * 10_000_000] = 1\nkeep = 2")
    assert r.ok, r.error
    assert r.outputs == {"keep": 2}
    assert len(r.outputs_skipped) == 1
    reported = next(iter(r.outputs_skipped))
    assert len(reported) <= 64, "the oversized name was echoed back whole"


def test_many_legal_hostcall_arguments_are_refused_together(make_session):
    """Individually legal, collectively enormous. A per-value ceiling
    cannot see an argument count, and the supervisor parses the whole
    request either way.

    Driven with small caps rather than large data: the property is the
    arithmetic, and making a VM allocate 90 MB to prove it would be a
    slow way to learn the same thing.
    """
    class Echo:
        def echo(self, *a):
            return len(a)

    with make_session(host_objects={"svc": Echo()},
                      allow={"svc": {"echo"}}) as s:
        r = s.python(
            "x = svc.echo(*['z' * 90_000 for _ in range(10)])",
            caps={"value": 100_000, "frame": 500_000},
        )
        assert not r.ok
        assert r.error.etype == "TypeError"
        assert "aggregate" in r.error.message

        # And one argument of the same size still goes through, so the
        # aggregate check is what refused it rather than the per-value one.
        ok = s.python("y = svc.echo('z' * 90_000)",
                      caps={"value": 100_000, "frame": 500_000})
        assert ok.ok, ok.error
        assert ok.outputs["y"] == 1


def test_an_oversized_emit_raises_where_it_was_called(session):
    """Emits are events, and a dropped event is indistinguishable from
    one that never fired. So this fails at the `emit()` the agent
    wrote, rather than vanishing the way a harvested binding does."""
    r = session.python("emit('big', 'z' * 20_000_000)")
    assert not r.ok
    assert r.error.etype == "ValueTooLarge"
    assert session.emits == []


def test_an_oversized_emit_name_is_refused_too(session):
    """The name rides the same body and is parsed by the same
    supervisor, so guarding only the value left `emit('n' * N, None)`
    as a way straight past the guard."""
    r = session.python("emit('n' * 200_000, None)", caps={"value": 100_000})
    assert not r.ok
    assert r.error.etype == "ValueTooLarge"
    assert "name" in r.error.message
    assert session.emits == []


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
