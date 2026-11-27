"""Direct tests for ``memtomem_stm.utils.document_metadata``."""

from __future__ import annotations

import pytest

from memtomem_stm.utils.document_metadata import metadata_json, metadata_text


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("plain", "plain"),
        ("a\nb", "a\\u000ab"),
        ("a\r\nb", "a\\u000d\\u000ab"),
        ("x`y", "x\\u0060y"),
        ("<t>", "\\u003ct\\u003e"),
        ("plain é", "plain é"),
        (5, "5"),
    ],
)
def test_metadata_text(value: object, expected: str) -> None:
    assert metadata_text(value) == expected


# The characters ``str.splitlines()`` splits on, written out here instead of read
# from the module so that dropping one from the module's set fails a case.
@pytest.mark.parametrize(
    "char",
    ["\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"],
)
def test_metadata_text_escapes_each_line_break(char: str) -> None:
    encoded = metadata_text(f"a{char}b")

    assert encoded == f"a\\u{ord(char):04x}b"
    assert len(encoded.splitlines()) == 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"k": "é"}, '{"k": "\\u00e9"}'),
        (["a\nb"], '["a\\nb"]'),
        (None, "null"),
    ],
)
def test_metadata_json(value: object, expected: str) -> None:
    assert metadata_json(value) == expected


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_metadata_json_rejects_non_finite_floats(value: float) -> None:
    with pytest.raises(ValueError):
        metadata_json(value)
