import pytest

from dud.values import (
    NotRepresentable,
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
