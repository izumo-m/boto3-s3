"""Unit tests for boto3_s3_cli.shorthand (``--metadata`` map parsing).

Pins the aws-cli shorthand corner cases the naive split/partition parser got
wrong: duplicate-key rejection, escaped commas, quoted values with commas,
and the ``@=`` paramfile operator
(triangulated against the aws-cli awscli/shorthand.py).

What a map of strings cannot hold - a list, a nested map, a JSON array or a
non-string JSON value, bytes from ``@=fileb://`` - aws parses and then rejects;
here it is refused while parsing. Either way it is a usage error (252).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from boto3_s3 import InvalidValueError, ValidationError
from boto3_s3_cli.shorthand import parse_map_option


def _parse(value: str) -> dict[str, str]:
    return parse_map_option(value, name="--metadata", operation="cp")


def _assert_parse_error(text: str) -> None:
    """A syntax error: rc 252's ValidationError naming the option, not a
    255 `InvalidValueError`."""
    with pytest.raises(ValidationError) as excinfo:
        _parse(text)
    assert not isinstance(excinfo.value, InvalidValueError)
    assert "--metadata" in str(excinfo.value)


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
        # Only whitespace may follow a quoted value before the next ','
        # (verified against the pinned aws-cli: rc 252).
        _assert_parse_error("title='hello' x")

    def test_json_object_form(self) -> None:
        assert _parse('{"a":"b","c":"d"}') == {"a": "b", "c": "d"}

    def test_empty_key_accepted_like_aws(self) -> None:
        # aws-cli's _keyval has no empty-key guard: with the cursor on "=" the
        # key is "" and the pair parses (verified against the real binary:
        # `aws s3 cp ... --metadata "=bar" --dryrun` proceeds, rc 0).
        assert _parse("=bar") == {"": "bar"}
        assert _parse("foo=1,=bar") == {"foo": "1", "": "bar"}


class TestCsvSecondValueProbe:
    """aws's ``_csv_value`` second-value probe and its backtracking.

    An empty segment between pairs is absorbed, a trailing comma is aws's
    ``'<second>'`` parse error, and a second value that does parse would build
    a csv list, which is refused. Accepted inputs differentially verified
    against the pinned aws-cli."""

    def test_empty_segment_between_pairs_is_absorbed(self) -> None:
        # The failed probe backtracks only to the nearest ',', so the empty
        # segment vanishes and both pairs parse (measured rc 0 on the pinned
        # aws where the plain pair loop errored 252).
        assert _parse("a=b,,c=d") == {"a": "b", "c": "d"}

    def test_empty_segment_with_whitespace_is_absorbed(self) -> None:
        assert _parse("a=b , , c=d") == {"a": "b", "c": "d"}

    def test_repeated_empty_segments_are_absorbed(self) -> None:
        assert _parse("x=1,,y=2,,z=3") == {"x": "1", "y": "2", "z": "3"}

    def test_empty_value_before_an_empty_segment(self) -> None:
        assert _parse("a=,,b=c") == {"a": "", "b": "c"}

    def test_backtrack_lands_inside_an_escaped_comma(self) -> None:
        # The probe consumes the escaped comma "\," as its second value; when
        # the following '=' fails the csv-list extension, aws backtracks to
        # the nearest ',' - the one INSIDE the escape - and the pair loop
        # re-reads "a=aa" from there.
        assert _parse(r"-=,\,a=aa") == {"-": "", "a": "aa"}

    def test_trailing_comma_is_the_second_value_parse_error(self) -> None:
        # An at-EOF probe failure propagates (measured live: rc 252).
        _assert_parse_error("a=b,")

    def test_double_trailing_comma_still_errors(self) -> None:
        # The first ',' probe backtracks, the pair loop consumes the second,
        # and EOF fails the next pair's '=' expectation.
        _assert_parse_error("a=b,,")

    @pytest.mark.parametrize("value", ["a=b,c", "a=b,c,d=e", "a=b, c"])
    def test_csv_list_is_refused(self, value: str) -> None:
        # A second value that parses would form a csv list, which a map of
        # strings cannot hold (aws parses it, then rejects it - 252 too).
        with pytest.raises(ValidationError, match="A value cannot be a list"):
            _parse(value)


class TestNonScalarShapes:
    """The grammar's list and hash constructs, refused where they start.

    aws parses ``k=[a,b]`` and ``k={a=b}`` - they are legal shorthand - and
    only then discovers that a map of strings cannot hold them; both end at
    252.
    """

    @pytest.mark.parametrize("value", ["k=[a,b]", "k=[]", "k={a=b}", "k={a=[b,c]}", "a=1,c=[3"])
    def test_list_and_hash_values_are_refused(self, value: str) -> None:
        with pytest.raises(ValidationError, match="must be quoted"):
            _parse(value)

    def test_quoted_bracket_value_is_a_string(self) -> None:
        assert _parse("k=\"[a,b]\",j='{c}'") == {"k": "[a,b]", "j": "{c}"}

    def test_opening_bracket_inside_a_value_stays_scalar(self) -> None:
        # The list / hash dispatch looks only at the first character of the
        # value, so an opening bracket further in is an ordinary character.
        # The closing ones are not: they fall outside aws's value character
        # class and end the value, which is why `k=x[y]z` is a parse error.
        assert _parse("k=a[b") == {"k": "a[b"}
        assert _parse("k=a{b,j=c") == {"k": "a{b", "j": "c"}
        _assert_parse_error("k=x[y]z")


class TestAtEqualsParamfile:
    """The ``@=`` operator (aws grammar ``key "@=" [file-optional-values]``):
    ``file://`` loads text, a prefix-less value passes through (verified
    against the pinned aws-cli: ``--metadata a@=file://f`` parses and
    transfers), and ``fileb://`` bytes are refused."""

    def test_plain_value_passes_through(self) -> None:
        assert _parse("a@=v") == {"a": "v"}

    def test_file_prefix_loads_text(self, tmp_path: Path) -> None:
        ref = tmp_path / "val.txt"
        ref.write_text("loaded")
        assert _parse(f"a@=file://{ref}") == {"a": "loaded"}

    def test_fileb_prefix_is_refused_as_a_non_string_value(self, tmp_path: Path) -> None:
        # A map value must be a string, so a fileb:// bytes load is a usage
        # error (rc 252) - never a transfer.
        ref = tmp_path / "val.bin"
        ref.write_bytes(b"\x00\x01")
        with pytest.raises(ValidationError) as excinfo:
            _parse(f"a@=fileb://{ref}")
        assert str(excinfo.value) == (
            "Error parsing parameter '--metadata': the value of key 'a' must be "
            "text, not the bytes of a fileb:// file"
        )

    def test_missing_paramfile_is_the_options_parse_error(self, tmp_path: Path) -> None:
        # A file named on the command line that cannot be read is a usage
        # error naming the option (252), as for any other argument - aws's
        # shorthand parser alone loads it outside that handler (255 there).
        with pytest.raises(ValidationError) as excinfo:
            _parse(f"a@=file://{tmp_path}/no-such-file")
        assert not isinstance(excinfo.value, InvalidValueError)
        assert str(excinfo.value).startswith("Error parsing parameter '--metadata': ")
        assert f"file://{tmp_path}/no-such-file" in str(excinfo.value)

    def test_undecodable_paramfile_is_the_options_parse_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref = tmp_path / "val.bin"
        ref.write_bytes(b"\xff\xfe\x00bin")
        # Pin the decode to UTF-8 (aws's compat_open knob): the locale default
        # elsewhere may be a codec that decodes any byte (cp1252 on Windows).
        monkeypatch.setenv("AWS_CLI_FILE_ENCODING", "utf-8")
        with pytest.raises(ValidationError) as excinfo:
            _parse(f"a@=file://{ref}")
        assert not isinstance(excinfo.value, InvalidValueError)
        assert str(ref) in str(excinfo.value)

    def test_at_without_equals_is_a_parse_error(self) -> None:
        # "a@b=c": the '@' is consumed as the operator probe, then the '='
        # expectation lands on 'b'.
        _assert_parse_error("a@b=c")

    def test_mixed_with_plain_pairs(self, tmp_path: Path) -> None:
        ref = tmp_path / "v.txt"
        ref.write_text("x")
        assert _parse(f"k=1,a@=file://{ref}") == {"k": "1", "a": "x"}


class TestJsonRoute:
    """A value led by ``[`` or ``{`` (after whitespace) is read as JSON, and
    only an object whose values are all strings is a map."""

    @pytest.mark.parametrize("value", ["[]", "[1,2]", '["a"]', "[a=b]", "[", "   []  "])
    def test_bracket_lead_is_refused(self, value: str) -> None:
        with pytest.raises(ValidationError, match="expected a JSON object"):
            _parse(value)

    def test_brace_lead_takes_the_json_route(self) -> None:
        assert _parse('  {"a": "b"}') == {"a": "b"}

    @pytest.mark.parametrize("value", ['{"a": 1}', '{"a": "ok", "b": null}', '{"a": {}}'])
    def test_non_string_values_are_refused(self, value: str) -> None:
        with pytest.raises(ValidationError, match="must be a string"):
            _parse(value)


class TestJsonDecodeFailure:
    """A JSON document that does not decode is a usage error (252)."""

    def test_it_names_the_parameter_and_echoes_the_value(self) -> None:
        value = '{"a": 1,}'
        with pytest.raises(ValidationError) as excinfo:
            _parse(value)
        message = str(excinfo.value)
        assert "--metadata" in message
        assert value in message


class TestParseMapOptionErrors:
    def test_duplicate_key_rejected(self) -> None:
        # Silent last-write-wins was the bug; aws rejects with rc 252, and the
        # report names the option and the repeated key.
        with pytest.raises(ValidationError) as excinfo:
            _parse("k=1,k=2")
        assert "--metadata" in str(excinfo.value)
        assert '"k"' in str(excinfo.value)

    def test_missing_equals_rejected(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _parse("foo")
        assert "--metadata" in str(excinfo.value)

    def test_leading_comma_rejected(self) -> None:
        # An empty key NOT followed by "=" still fails, through _expect, as
        # aws's `--metadata ",foo=1"` does (rc 252).
        _assert_parse_error(",foo=1")


class TestSyntaxErrors:
    """Values the grammar cannot read are the option's usage error (252)."""

    @pytest.mark.parametrize(
        "value",
        ["a'=b", "a=b,\nc==d", "k='a,b", 'k="a,b'],
        ids=["quote-in-key", "multiline", "unterminated-single", "unterminated-double"],
    )
    def test_it_is_rejected_naming_the_option(self, value: str) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _parse(value)
        assert "--metadata" in str(excinfo.value)
