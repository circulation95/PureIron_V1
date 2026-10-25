from __future__ import annotations

import re
import unicodedata
from typing import cast

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from pureiron_project.canonical import CanonicalError, Json, as_json_roundtrip, dump, load

MAX = 2**53 - 1


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("a: 1\na: 2\n", "duplicate"),
        ("a: &x 1\nb: *x\n", "anchors"),
        ("base: &b {x: 1}\n", "anchors"),
        ("a: !!str 1\n", "tags"),
        ("a: !custom x\n", "tags"),
        ("1: x\n", "keys must be strings"),
        ("a: 2026-10-04\n", "unsupported YAML value"),
        ("a: .nan\n", "NaN"),
        ("a: .inf\n", "NaN"),
        (f"a: {MAX + 1}\n", "2^53"),
        ("a: [1, 2\n", "<input>"),
        ("a: 0x_\n", "<input>"),
        ("a: +_\n", "<input>"),
    ],
)
def test_strict_loader_rejects(text: str, fragment: str) -> None:
    with pytest.raises(CanonicalError, match=re.escape(fragment)):
        load(text)


def test_yaml_12_scalars_and_normalisation() -> None:
    data = load("\ufeffa: yes\r\nb: on\r\nc: 0o17\r\nd: cafe\u0301\r\ne: ~\r\n")
    assert data == {"a": "yes", "b": "on", "c": 15, "d": "caf\u00e9", "e": None}


def test_dump_is_sorted_block_yaml_with_jcs_numbers() -> None:
    data: Json = {
        "z": [1.0, 0.5, 1e21],
        "a": {"k": "needs: quoting"},
        "m": [],
        "n": {},
        "s": "plain text",
    }
    assert dump(data) == (
        'a:\n  k: "needs: quoting"\nm: []\n"n": {}\ns: plain text\nz:\n  - 1\n  - 0.5\n  - 1e+21\n'
    )


@pytest.mark.parametrize(
    "value",
    [
        "",
        " lead",
        "trail ",
        "yes",
        "On",
        "N",
        "null",
        "~",
        "123",
        "1.5",
        "-x",
        "#c",
        "a: b",
        "a #b",
        "line\nbreak",
        "true",
        "+_",
        "0x_",
        "0b_",
        "0o_",
        "._",
        "+._",
    ],
)
def test_ambiguous_strings_are_quoted_and_roundtrip(value: str) -> None:
    text = dump({"k": value})
    assert text.startswith('k: "')
    assert load(text) == {"k": value}


_keys = st.text(min_size=1, max_size=12)
_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-MAX, max_value=MAX),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=20),
)
_json: st.SearchStrategy[Json] = st.recursive(
    _scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(_keys, children, max_size=4),
    ),
    max_leaves=12,
)


@settings(max_examples=1000, deadline=None)
@given(_json)
@example({"+_": None})
@example(["0x_", "._"])
def test_roundtrip_and_idempotence(value: Json) -> None:
    """parse(dump(x)) == NFC(x) (JCS 의미에서), dump는 고정점."""
    expected = _nfc(value)
    if expected is _COLLIDES:
        with pytest.raises(CanonicalError, match="duplicate key after NFC"):
            dump(value)
        return
    text = dump(value)
    assert as_json_roundtrip(load(text)) == as_json_roundtrip(cast(Json, expected))
    assert dump(load(text)) == text


_COLLIDES = object()


def _nfc(value: Json) -> object:
    """기대값: 문자열·키 NFC. 서로 다른 키가 NFC 후 같아지면 _COLLIDES."""
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, list):
        items = [_nfc(v) for v in value]
        return _COLLIDES if _COLLIDES in items else items
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for key, item in value.items():
            nkey = unicodedata.normalize("NFC", key)
            nitem = _nfc(item)
            if nkey in out or nitem is _COLLIDES:
                return _COLLIDES
            out[nkey] = nitem
        return out
    return value
