"""Unit tests for boto3_s3_cli.shorthand (``--metadata`` map parsing).

Pins the aws-cli shorthand corner cases the naive split/partition parser got
wrong: duplicate-key rejection, escaped commas, quoted values with commas,
and the ``@=`` paramfile operator
(triangulated against the aws-cli awscli/shorthand.py).

It also pins the two boundaries around the grammar, both of which decide which
*wording* the user sees rather than the exit code: the JSON short-circuit that
keeps ``[``- and ``{``-led values away from the parser entirely, and the schema
report that answers a value the grammar accepts but a map of strings cannot
hold. Every expected string here was measured byte for byte against the pinned
aws-cli, and the parser itself was differentially fuzzed against aws-cli's
``ShorthandParser`` over ~750k generated inputs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from boto3_s3 import InvalidValueError, ValidationError
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


class TestCsvSecondValueProbe:
    """aws's ``_csv_value`` second-value probe and its backtracking.

    An empty segment between pairs is absorbed, a trailing comma is aws's
    ``'<second>'`` parse error, and a second value that does parse builds a
    csv list. All expectations differentially verified against the pinned
    aws-cli."""

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
        # An at-EOF probe failure propagates with aws's exact wording
        # (measured live).
        with pytest.raises(ValidationError) as excinfo:
            _parse("a=b,")
        assert str(excinfo.value) == (
            "Error parsing parameter '--metadata': "
            "Expected: '<second>', received: '<none>' for input:\n"
            " a=b,\n"
            "    ^"
        )

    def test_double_trailing_comma_still_errors(self) -> None:
        # The first ',' probe backtracks, the pair loop consumes the second,
        # and EOF fails the next pair's '=' expectation.
        with pytest.raises(ValidationError, match="Expected: '=', received: 'EOF'"):
            _parse("a=b,,")

    def test_csv_list_shapes_reach_the_schema_report(self) -> None:
        # A second value that parses forms a csv list - a well-formed value the
        # string map cannot hold, so it is the schema's error, not the parser's.
        with pytest.raises(ValidationError) as excinfo:
            _parse("a=b,c")
        assert str(excinfo.value) == (
            "Parameter validation failed:\n"
            "Invalid type for parameter a, value: ['b', 'c'], "
            "type: <class 'list'>, valid types: <class 'str'>"
        )
        with pytest.raises(ValidationError) as excinfo:
            _parse("a=b,c,d=e")
        assert str(excinfo.value) == (
            "Parameter validation failed:\n"
            "Invalid type for parameter a, value: ['b', 'c'], "
            "type: <class 'list'>, valid types: <class 'str'>"
        )


class TestNonScalarShapes:
    """The grammar's list and hash constructs, and the report they draw.

    aws parses ``k=[a,b]`` and ``k={a=b}`` happily - they are legal shorthand -
    and only then discovers that a map of strings cannot hold them. The user
    therefore gets botocore's type complaint, naming the key and printing the
    parsed structure, rather than a syntax error with a caret. Every expected
    string measured against the pinned aws-cli.
    """

    def test_explicit_list(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _parse("k=[a,b]")
        assert str(excinfo.value) == (
            "Parameter validation failed:\n"
            "Invalid type for parameter k, value: ['a', 'b'], "
            "type: <class 'list'>, valid types: <class 'str'>"
        )

    def test_empty_explicit_list(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _parse("k=[]")
        assert str(excinfo.value) == (
            "Parameter validation failed:\n"
            "Invalid type for parameter k, value: [], "
            "type: <class 'list'>, valid types: <class 'str'>"
        )

    def test_hash_literal(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _parse("k={a=b}")
        assert str(excinfo.value) == (
            "Parameter validation failed:\n"
            "Invalid type for parameter k, value: {'a': 'b'}, "
            "type: <class 'dict'>, valid types: <class 'str'>"
        )

    def test_nested_constructs_render_as_plain_python_containers(self) -> None:
        # Unlike the JSON route there is no OrderedDict here: the shorthand
        # parser builds plain dicts and lists, and aws prints their bare repr.
        with pytest.raises(ValidationError) as excinfo:
            _parse("k={a=[b,c],d={e=f}}")
        assert str(excinfo.value) == (
            "Parameter validation failed:\n"
            "Invalid type for parameter k, value: {'a': ['b', 'c'], 'd': {'e': 'f'}}, "
            "type: <class 'dict'>, valid types: <class 'str'>"
        )

    def test_hash_literal_repeats_a_key_by_overwriting(self) -> None:
        # Only the top level rejects a duplicate; aws's _hash_literal has no
        # such guard, so the last write wins (measured: value ends up {'a': 'c'}).
        with pytest.raises(ValidationError) as excinfo:
            _parse("k={a=b,a=c}")
        assert str(excinfo.value) == (
            "Parameter validation failed:\n"
            "Invalid type for parameter k, value: {'a': 'c'}, "
            "type: <class 'dict'>, valid types: <class 'str'>"
        )

    def test_report_names_every_offending_key_in_map_order(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _parse("a=1,2,b=ok,c=[3,4]")
        assert str(excinfo.value) == (
            "Parameter validation failed:\n"
            "Invalid type for parameter a, value: ['1', '2'], "
            "type: <class 'list'>, valid types: <class 'str'>\n"
            "Invalid type for parameter c, value: ['3', '4'], "
            "type: <class 'list'>, valid types: <class 'str'>"
        )

    def test_opening_bracket_inside_a_value_stays_scalar(self) -> None:
        # The list / hash dispatch looks only at the first character of the
        # value, so an opening bracket further in is an ordinary character.
        # The closing ones are not: they fall outside aws's value character
        # class and end the value, which is why `k=x[y]z` is a parse error.
        assert _parse("k=a[b") == {"k": "a[b"}
        assert _parse("k=a{b,j=c") == {"k": "a{b", "j": "c"}
        with pytest.raises(ValidationError, match=r"Expected: ',', received: '\]'"):
            _parse("k=x[y]z")


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
        # ParamValidation (rc 252, measured) - never a transfer. The report is
        # botocore's schema wording, with no "Error parsing parameter" prefix.
        ref = tmp_path / "val.bin"
        ref.write_bytes(b"\x00\x01")
        with pytest.raises(ValidationError) as excinfo:
            _parse(f"a@=fileb://{ref}")
        assert str(excinfo.value) == (
            "Parameter validation failed:\n"
            "Invalid type for parameter a, value: b'\\x00\\x01', "
            "type: <class 'bytes'>, valid types: <class 'str'>"
        )

    def test_missing_paramfile_is_the_general_error(self, tmp_path: Path) -> None:
        # aws's shorthand parser calls get_paramfile directly, outside the
        # load-cli-arg handler that names the option, so the failure reaches its
        # general handler: rc 255 with the bare message, not the parser's 252
        # (measured: `--metadata k@=file:///no/x`).
        with pytest.raises(InvalidValueError) as excinfo:
            _parse(f"a@=file://{tmp_path}/no-such-file")
        assert str(excinfo.value).startswith("Unable to load paramfile file://")

    def test_undecodable_paramfile_is_the_general_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref = tmp_path / "val.bin"
        ref.write_bytes(b"\xff\xfe\x00bin")
        # Pin the decode to UTF-8 (aws's compat_open knob): the locale default
        # elsewhere may be a codec that decodes any byte (cp1252 on Windows).
        monkeypatch.setenv("AWS_CLI_FILE_ENCODING", "utf-8")
        with pytest.raises(InvalidValueError) as excinfo:
            _parse(f"a@=file://{ref}")
        assert str(excinfo.value) == (
            f"Unable to load paramfile ({ref}), text contents could not be decoded.  "
            "If this is a binary file, please use the fileb:// prefix instead of the "
            "file:// prefix."
        )

    def test_at_without_equals_is_a_parse_error(self) -> None:
        # "a@b=c": the '@' is consumed as the operator probe, then the '='
        # expectation lands on 'b' - aws's "Expected: '='" wording.
        with pytest.raises(ValidationError, match="Expected: '='"):
            _parse("a@b=c")

    def test_mixed_with_plain_pairs(self, tmp_path: Path) -> None:
        ref = tmp_path / "v.txt"
        ref.write_text("x")
        assert _parse(f"k=1,a@=file://{ref}") == {"k": "1", "a": "x"}

    def test_operator_reaches_every_element_of_a_list(self, tmp_path: Path) -> None:
        # aws arms the operator per pair, not per value, so each element of the
        # csv / explicit list is resolved; the list itself is then the schema's
        # error (measured).
        ref = tmp_path / "v.txt"
        ref.write_text("loaded")
        with pytest.raises(ValidationError) as excinfo:
            _parse(f"a@=[file://{ref},plain]")
        assert str(excinfo.value) == (
            "Parameter validation failed:\n"
            "Invalid type for parameter a, value: ['loaded', 'plain'], "
            "type: <class 'list'>, valid types: <class 'str'>"
        )

    def test_operator_is_disarmed_inside_a_hash_literal(self, tmp_path: Path) -> None:
        # aws's _hash_literal re-arms the operator per inner key, so the outer
        # "@=" does not carry into the nested values.
        ref = tmp_path / "v.txt"
        ref.write_text("loaded")
        with pytest.raises(ValidationError) as excinfo:
            _parse(f"a@={{b=file://{ref}}}")
        assert str(excinfo.value) == (
            "Parameter validation failed:\n"
            f"Invalid type for parameter a, value: {{'b': 'file://{ref}'}}, "
            "type: <class 'dict'>, valid types: <class 'str'>"
        )


class TestJsonShortCircuitGate:
    """aws's ``_should_parse_as_shorthand`` short-circuit and the branch behind it.

    A value whose leading non-whitespace character is ``[`` or ``{`` never
    reaches the shorthand parser. The map branch it lands in then accepts only
    ``{``, so a ``[`` lead gets a bare ``Invalid JSON:`` followed by the value
    verbatim - no decoder detail, no ``JSON received:`` line, and no lstrip.
    Measured byte for byte against the pinned aws-cli.
    """

    @pytest.mark.parametrize("value", ["[]", "[1,2]", '["a"]', "[a=b]", "[", '[{"a":"b"}]', "[1,]"])
    def test_bracket_lead_is_the_bare_invalid_json_report(self, value: str) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _parse(value)
        assert str(excinfo.value) == (
            f"Error parsing parameter '--metadata': Invalid JSON:\n{value}"
        )

    def test_echoed_value_keeps_its_surrounding_whitespace(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _parse("   []  ")
        assert str(excinfo.value) == (
            "Error parsing parameter '--metadata': Invalid JSON:\n   []  "
        )

    def test_brace_lead_still_takes_the_json_route(self) -> None:
        assert _parse('  {"a": "b"}') == {"a": "b"}


class TestJsonDecodeMessagePin:
    """The decoder wording is pinned to the official aws build's Python (3.14).

    Python 3.13 gave the two trailing-comma shapes their own message, anchored
    on the comma rather than on the closing bracket; older interpreters are
    re-anchored so the CLI prints the same text everywhere. A corpus of ~930
    broken documents run under 3.10 / 3.12 / 3.14 showed those two shapes are
    the whole difference, so nothing else is rewritten.
    """

    def test_trailing_comma_in_object(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _parse('{"a": 1,}')
        assert str(excinfo.value) == (
            "Error parsing parameter '--metadata': Invalid JSON: "
            "Illegal trailing comma before end of object: line 1 column 8 (char 7)\n"
            'JSON received: {"a": 1,}'
        )

    def test_trailing_comma_in_nested_array(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _parse('{"a": [1,]}')
        assert str(excinfo.value) == (
            "Error parsing parameter '--metadata': Invalid JSON: "
            "Illegal trailing comma before end of array: line 1 column 9 (char 8)\n"
            'JSON received: {"a": [1,]}'
        )

    def test_comma_position_is_kept_across_whitespace_and_newlines(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _parse('{"a": 1,\n  }')
        assert str(excinfo.value) == (
            "Error parsing parameter '--metadata': Invalid JSON: "
            "Illegal trailing comma before end of object: line 1 column 8 (char 7)\n"
            'JSON received: {"a": 1,\n  }'
        )

    def test_comma_position_counts_from_the_last_newline(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _parse('{\n "a": 1,\n}')
        assert str(excinfo.value) == (
            "Error parsing parameter '--metadata': Invalid JSON: "
            "Illegal trailing comma before end of object: line 2 column 8 (char 9)\n"
            'JSON received: {\n "a": 1,\n}'
        )

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            ('{"a": 1,,}', "Expecting property name enclosed in double quotes"),
            ('{"a": [1,,2]}', "Expecting value"),
            ("{,}", "Expecting property name enclosed in double quotes"),
            ('{"a": [,]}', "Expecting value"),
            ('{"a" 1}', "Expecting ':' delimiter"),
        ],
    )
    def test_non_trailing_comma_failures_keep_the_decoder_wording(
        self, value: str, message: str
    ) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _parse(value)
        assert message in str(excinfo.value)
        assert "Illegal trailing comma" not in str(excinfo.value)


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

    def test_json_non_string_values_get_the_schema_report(self) -> None:
        # aws parses the JSON and then fails botocore's schema validation -
        # no "Error parsing parameter" prefix, one line per offending key
        # (measured against the pinned aws-cli).
        with pytest.raises(ValidationError) as excinfo:
            _parse('{"a": 1}')
        assert str(excinfo.value) == (
            "Parameter validation failed:\n"
            "Invalid type for parameter a, value: 1, type: <class 'int'>, "
            "valid types: <class 'str'>"
        )

    def test_json_schema_report_names_every_offending_key(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _parse('{"a": 1, "b": "ok", "c": true}')
        assert str(excinfo.value) == (
            "Parameter validation failed:\n"
            "Invalid type for parameter a, value: 1, type: <class 'int'>, "
            "valid types: <class 'str'>\n"
            "Invalid type for parameter c, value: True, type: <class 'bool'>, "
            "valid types: <class 'str'>"
        )

    def test_json_nested_values_render_in_ordereddict_form(self) -> None:
        # aws parses with object_pairs_hook=OrderedDict and its official
        # build (Python 3.14) prints the 3.12+ OrderedDict repr; the hand
        # formatter reproduces that text on every supported interpreter
        # (measured byte-equal against the pinned aws-cli, empty map and
        # deep nesting included).
        with pytest.raises(ValidationError) as excinfo:
            _parse('{"a": {"b": {"c": 1}}, "d": [1, "x", {"e": 2}], "f": {}}')
        assert str(excinfo.value) == (
            "Parameter validation failed:\n"
            "Invalid type for parameter a, value: OrderedDict({'b': OrderedDict({'c': 1})}), "
            "type: <class 'collections.OrderedDict'>, valid types: <class 'str'>\n"
            "Invalid type for parameter d, value: [1, 'x', OrderedDict({'e': 2})], "
            "type: <class 'list'>, valid types: <class 'str'>\n"
            "Invalid type for parameter f, value: OrderedDict(), "
            "type: <class 'collections.OrderedDict'>, valid types: <class 'str'>"
        )


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
