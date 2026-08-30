import pytest

from dud.values import (
    NotRepresentable,
    ValueTooLarge,
    _encode_sized,
    decode_map,
    decode_value,
    encode_map,
    encode_value,
    file_ref,
)


def test_json_roundtrip():
    for v in [None, True, 42, 3.14, "hi", [1, "a"], {"k": [1, 2]}]:
        assert decode_value(encode_value(v)) == v


def test_bytes_roundtrip():
    b = b"\x00\x01binary"
    tagged = encode_value(b)
    assert tagged["t"] == "bytes"
    assert decode_value(tagged) == b


def test_file_ref_decodes_to_path():
    assert decode_value(file_ref("out/plot.png")) == "out/plot.png"


def test_not_representable():
    with pytest.raises(NotRepresentable):
        encode_value(object())


def test_encode_map_skips_and_records():
    enc, skipped = encode_map({"good": 1, "bad": object()})
    assert "good" in enc and skipped == {"bad": "object"}
    assert decode_map(enc) == {"good": 1}


# ---- size guards --------------------------------------------------------


def test_sized_measures_the_wire_form_not_the_object():
    """Size is what crosses, which for bytes is the base64 expansion.

    Measuring `len(v)` would under-count binary by a third, and the
    guard exists to bound what the supervisor parses, not what the
    guest happens to hold.
    """
    _tagged, size = _encode_sized(b"\x00" * 300)
    assert size == 400  # 300 bytes of base64 is 400 characters
    _tagged, text = _encode_sized("x" * 300)
    assert text == 302  # the two quotes count; they are on the wire too


def test_no_cap_is_the_old_behavior():
    """The host side passes no cap, and must be unchanged by all this."""
    assert encode_value("z" * 5_000_000)["v"] == "z" * 5_000_000


def test_encode_value_refuses_an_oversized_value():
    with pytest.raises(ValueTooLarge) as e:
        encode_value("z" * 5000, cap=1000)
    assert "over the" in str(e.value)
    assert "workspace file" in str(e.value)  # says what to do instead


def test_too_large_is_catchable_as_not_representable():
    """Everything already written to skip what cannot cross must also
    skip what should not, without being taught the new type."""
    assert issubclass(ValueTooLarge, NotRepresentable)
    with pytest.raises(NotRepresentable):
        encode_value("z" * 5000, cap=1000)


def test_encode_map_records_the_size_it_refused():
    enc, skipped = encode_map({"big": "z" * 5000, "small": "ok"}, cap=1000)
    assert list(enc) == ["small"]
    assert skipped["big"].startswith("str (")
    assert "per-value limit" in skipped["big"]


def test_a_total_skips_only_what_does_not_fit():
    """A big binding must not cost the caller the small one after it.

    Stopping the walk at the first overflow would make what comes back
    depend on the order bindings happen to appear in, which is not
    something the caller controls or can reason about.
    """
    enc, skipped = encode_map(
        {"a": "x" * 400, "big": "z" * 900, "c": "y" * 400}, total=1000
    )
    assert sorted(enc) == ["a", "c"]
    assert list(skipped) == ["big"]
    assert "total" in skipped["big"]


def test_the_per_value_limit_is_named_before_the_total():
    """Two ways to be refused, and the message has to pick the true
    one: a value over the per-value cap is over it whether or not any
    total was in play, and saying "the total" would send someone to
    raise the wrong number."""
    _enc, skipped = encode_map({"big": "z" * 5000}, cap=1000, total=1_000_000)
    assert "per-value limit" in skipped["big"]


def test_a_binding_name_counts_toward_the_limits():
    """The name is on the wire beside the value, and nothing was
    measuring it: `globals()['k' * 40_000_000] = 1` charged one byte to
    the total and put 40 MB in the frame."""
    enc, skipped = encode_map({"k" * 5000: 1, "ok": 2}, cap=1000)
    assert list(enc) == ["ok"]
    assert len(skipped) == 1


def test_an_oversized_name_is_not_reported_under_itself():
    """`skipped` rides the very frame the caller is being warned about,
    so filing a 40 MB name under itself would put that name on the wire
    anyway — the guard causing the problem it reports."""
    _enc, skipped = encode_map({"k" * 5000: 1}, cap=1000)
    reported = next(iter(skipped))
    assert len(reported) <= 64 and reported.endswith("...")


def test_a_short_name_is_reported_verbatim():
    _enc, skipped = encode_map({"df": object()})
    assert skipped == {"df": "object"}
