"""Unit tests for boto3_s3_cli.shorthand (``--metadata`` map parsing).

Pins the aws-cli shorthand corner cases the naive split/partition parser got
wrong: duplicate-key rejection, escaped commas, and quoted values with commas
(triangulated against the aws-cli awscli/shorthand.py).
"""

from __future__ import annotations

import pytest

from boto3_s3 import ValidationError
from boto3_s3_cli.shorthand import parse_map_option


def _parse(value: str) -> dict[str, str]:
    return parse_map_option(value, name="--metadata", operation="cp")


class TestParseMapOption:
    def test_single_pair(self) -> None:
        assert _parse("k=v") == {"k": "v"}

    def test_multiple_pairs_preserve_order(self) -> None:
        result = _parse("a=1,b=2,c=3")
        assert result == {"a": "1", "b": "2", "c": "3"}
        assert list(result) == ["a", "b", "c"]

    def test_empty_value(self) -> None:
        assert _parse("k=") == {"k": ""}

    def test_value_with_embedded_equals(self) -> None:
        assert _parse("k=a=b") == {"k": "a=b"}

    def test_escaped_comma_in_unquoted_value(self) -> None:
        assert _parse(r"k=a\,b,j=c") == {"k": "a,b", "j": "c"}

    def test_double_quoted_value_with_comma(self) -> None:
        assert _parse('k="a,b",j=c') == {"k": "a,b", "j": "c"}

    def test_single_quoted_value_with_comma(self) -> None:
        assert _parse("k='a,b',j=c") == {"k": "a,b", "j": "c"}

    def test_json_object_form(self) -> None:
        assert _parse('{"a":"b","c":"d"}') == {"a": "b", "c": "d"}


class TestParseMapOptionErrors:
    def test_duplicate_key_rejected(self) -> None:
        # Silent last-write-wins was the bug; aws rejects with rc 252.
        with pytest.raises(ValidationError) as excinfo:
            _parse("k=1,k=2")
        assert "Error parsing parameter '--metadata'" in str(excinfo.value)
        assert 'Second instance of key "k"' in str(excinfo.value)

    def test_missing_equals_rejected(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _parse("foo")
        assert "Error parsing parameter '--metadata'" in str(excinfo.value)

    def test_invalid_json_rejected(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _parse('{"a": }')
        assert "Error parsing parameter '--metadata'" in str(excinfo.value)

    def test_json_non_string_values_rejected(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _parse('{"a": 1}')
        assert "Error parsing parameter '--metadata'" in str(excinfo.value)
