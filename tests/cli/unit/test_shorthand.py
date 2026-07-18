"""Unit tests for boto3_s3_cli.shorthand (``--metadata`` map parsing).

Pins the aws-cli shorthand corner cases the naive split/partition parser got
wrong: duplicate-key rejection, escaped commas, quoted values with commas,
and the ``@=`` paramfile operator
(triangulated against the aws-cli awscli/shorthand.py).
"""

from __future__ import annotations

from pathlib import Path

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

    def test_trailing_whitespace_after_quoted_value(self) -> None:
        # aws's _csv_value consumes whitespace after the value before its
        # EOF/comma check (verified against the pinned aws-cli: rc 0). The
        # newline form is the natural shape of a file:// paramfile that keeps
        # its trailing newline.
        assert _parse("title='hello' ") == {"title": "hello"}
        assert _parse("title='hello'\n") == {"title": "hello"}
        assert _parse('k="a,b" ,j=c') == {"k": "a,b", "j": "c"}

    def test_non_comma_after_quoted_value_and_whitespace_rejected(self) -> None:
        # The whitespace is consumed first, so the error names the offending
        # character, matching aws's wording (verified against the pinned
        # aws-cli).
        with pytest.raises(ValidationError, match="Expected: ',', received: 'x'"):
            _parse("title='hello' x")

    def test_json_object_form(self) -> None:
        assert _parse('{"a":"b","c":"d"}') == {"a": "b", "c": "d"}

    def test_empty_key_accepted_like_aws(self) -> None:
        # aws-cli's _keyval has no empty-key guard: with the cursor on "=" the
        # key is "" and the pair parses (verified against the real binary:
        # `aws s3 cp ... --metadata "=bar" --dryrun` proceeds, rc 0).
        assert _parse("=bar") == {"": "bar"}
        assert _parse("foo=1,=bar") == {"foo": "1", "": "bar"}


class TestAtEqualsParamfile:
    """The ``@=`` operator (aws grammar ``key "@=" [file-optional-values]``):
    ``file://`` loads text, ``fileb://`` bytes, a prefix-less value passes
    through (verified against the pinned aws-cli: ``--metadata a@=file://f`` parses
    and transfers)."""

    def test_plain_value_passes_through(self) -> None:
        assert _parse("a@=v") == {"a": "v"}

    def test_file_prefix_loads_text(self, tmp_path: Path) -> None:
        ref = tmp_path / "val.txt"
        ref.write_text("loaded")
        assert _parse(f"a@=file://{ref}") == {"a": "loaded"}

    def test_fileb_prefix_is_rejected_as_a_non_string_value(self, tmp_path: Path) -> None:
        # aws schema-validates the shorthand result at parse time: a map value
        # must be a string, so a fileb:// bytes load is its pre-pipeline
        # ParamValidation (rc 252, measured) - never a transfer.
        ref = tmp_path / "val.bin"
        ref.write_bytes(b"\x00\x01")
        with pytest.raises(ValidationError, match="Invalid type for parameter a"):
            _parse(f"a@=fileb://{ref}")

    def test_missing_paramfile_is_a_usage_error(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _parse(f"a@=file://{tmp_path}/no-such-file")
        assert "Unable to load paramfile" in str(excinfo.value)

    def test_at_without_equals_is_a_parse_error(self) -> None:
        # "a@b=c": the '@' is consumed as the operator probe, then the '='
        # expectation lands on 'b' - aws's "Expected: '='" wording.
        with pytest.raises(ValidationError, match="Expected: '='"):
            _parse("a@b=c")

    def test_mixed_with_plain_pairs(self, tmp_path: Path) -> None:
        ref = tmp_path / "v.txt"
        ref.write_text("x")
        assert _parse(f"k=1,a@=file://{ref}") == {"k": "1", "a": "x"}


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

    def test_leading_comma_rejected_with_aws_wording(self) -> None:
        # An empty key NOT followed by "=" still fails, through _expect - the
        # same message the real aws prints for `--metadata ",foo=1"` (rc 252).
        with pytest.raises(ValidationError) as excinfo:
            _parse(",foo=1")
        assert "Expected: '=', received: ','" in str(excinfo.value)

    def test_invalid_json_rejected(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _parse('{"a": }')
        assert "Error parsing parameter '--metadata'" in str(excinfo.value)

    def test_json_non_string_values_rejected(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _parse('{"a": 1}')
        assert "Error parsing parameter '--metadata'" in str(excinfo.value)


class TestSyntaxErrorWordingParity:
    """Byte-exact aws parser wording for the shorthand syntax-error paths.

    Each expected string was measured against the pinned aws-cli
    (``aws s3 cp ... --metadata <input>``): aws single-quote-wraps the offending
    character literally (not via ``repr``), prefixes the echoed input line with
    one space, and places the caret with ``ShorthandParseError._error_location``
    (column counted from the last newline, value split around the next one).
    """

    def test_single_quote_actual_is_literal_wrapped(self) -> None:
        # repr would render the offending "'" as "\"'\"" - aws wraps it in bare
        # single quotes, so the tripled quote is the byte-exact form.
        with pytest.raises(ValidationError) as excinfo:
            _parse("a'=b")
        assert str(excinfo.value) == (
            "Error parsing parameter '--metadata': Expected: '=', received: ''' for input:\n"
            " a'=b\n"
            " ^"
        )

    def test_leading_space_on_echoed_input_line(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _parse(",foo=1")
        assert str(excinfo.value) == (
            "Error parsing parameter '--metadata': Expected: '=', received: ',' for input:\n"
            " ,foo=1\n"
            "^"
        )

    def test_eof_branch_wording(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _parse("foo")
        assert str(excinfo.value) == (
            "Error parsing parameter '--metadata': Expected: '=', received: 'EOF' for input:\n"
            " foo\n"
            "   ^"
        )

    def test_multiline_caret_reanchored_after_last_newline(self) -> None:
        # A shell can embed a newline in the argument; aws recomputes the column
        # from the last newline and echoes the offending line under the caret.
        with pytest.raises(ValidationError) as excinfo:
            _parse("a=b,\nc==d")
        assert str(excinfo.value) == (
            "Error parsing parameter '--metadata': Expected: ',', received: '=' for input:\n"
            " a=b,\n"
            "c==d\n"
            "  ^"
        )

    def test_unterminated_single_quote_uses_aws_regex_name(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _parse("k='a,b")
        assert str(excinfo.value) == (
            "Error parsing parameter '--metadata': "
            "Expected: '<singled quoted>', received: '<none>' for input:\n"
            " k='a,b\n"
            "  ^"
        )

    def test_unterminated_double_quote_uses_aws_regex_name(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _parse('k="a,b')
        assert str(excinfo.value) == (
            "Error parsing parameter '--metadata': "
            "Expected: '<double quoted>', received: '<none>' for input:\n"
            ' k="a,b\n'
            "  ^"
        )
