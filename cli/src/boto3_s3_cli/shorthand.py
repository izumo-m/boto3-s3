"""Parsing for aws-cli "map" option values (``--metadata KeyName1=string,...``).

aws-cli parses map-typed options with its shorthand grammar (``awscli/
shorthand.py`` ``ShorthandParser``) and also accepts the equivalent JSON object.
`_Parser` ports the part of that grammar a map of strings can take, so the CLI
accepts exactly what aws accepts, including the corner cases the naive
``split(",")``/``partition("=")`` got wrong:

- a duplicate key is rejected (``Second instance of key ...`` -> rc 252), not
  silently last-write-wins,
- backslash-escaped commas in an unquoted value (``k=a\\,b`` -> ``a,b``),
- single/double-quoted values that contain commas (``k="a,b"`` -> ``a,b``),
- an empty segment between pairs is absorbed (``a=b,,c=d`` is two pairs),
- the ``@=`` paramfile operator (``k@=file://path`` loads the file's text; a
  prefix-less value passes through - aws's "file-optional-values").

aws's grammar also builds lists (``k=a,b``, ``k=[a,b]``) and nested maps
(``k={a=b}``), which a map of strings cannot hold; aws parses them and then
rejects them with botocore's type report. Here they are rejected while
parsing, as is a ``fileb://`` value loaded through ``@=``. Every rejection is
an ``Error parsing parameter '<name>'`` usage error (rc 252 via
``ValidationError``), a failure to *load* an ``@=`` paramfile included
(`paramfile.named_argument`), like a paramfile named by any other argument.
"""

from __future__ import annotations

import json
import re
import string
from typing import TypeAlias, cast

from boto3_s3 import ValidationError
from boto3_s3_cli import paramfile, usage

# What one shorthand value can carry: text, or the bytes of an `@=fileb://`
# load (refused by `parse_map_option` once the whole value has parsed).
_ShorthandValue: TypeAlias = "str | bytes"

# Keys are alphanumeric plus ``-_.#/:`` (aws-cli awscli/shorthand.py); any other
# character terminates the key and is read as a delimiter or a syntax error.
_KEY_CHARS: frozenset[str] = frozenset(string.ascii_letters + string.digits + "-_.#/:")

# Value regex sources adapted from awscli/shorthand.py. The non-ASCII upper bound
# is aws-cli's literal U+FFFF, built with chr() so the source stays ASCII-only.
_MAX_CHAR = chr(0xFFFF)
_ESCAPED_COMMA = r"(\\,)"
_START_WORD = r"\!\#-&\(-\+\--\<\>-Z\\-z" + "|-" + _MAX_CHAR
_FIRST_FOLLOW_CHARS = r"\s\!\#-&\(-\+\--\\\^-\|~-" + _MAX_CHAR
_FIRST_VALUE_RE = re.compile(
    f"({_ESCAPED_COMMA}|[{_START_WORD}])({_ESCAPED_COMMA}|[{_FIRST_FOLLOW_CHARS}])*",
    re.UNICODE,
)
# The csv second-value fragment (aws-cli's _SECOND_VALUE): its follow set stops
# at '<' and resumes at '>', so '=' terminates a candidate - which is what makes
# the probe in `_csv_value` fail over to the next pair.
_SECOND_FOLLOW_CHARS = r"\s\!\#-&\(-\+\--\<\>-" + _MAX_CHAR
_SECOND_VALUE_RE = re.compile(
    f"({_ESCAPED_COMMA}|[{_START_WORD}])({_ESCAPED_COMMA}|[{_SECOND_FOLLOW_CHARS}])*",
    re.UNICODE,
)
_SINGLE_QUOTED_RE = re.compile(r"'(?:\\'|[^'])*'", re.UNICODE)
_DOUBLE_QUOTED_RE = re.compile(r'"(?:\\"|[^"])*"', re.UNICODE)


class _ShorthandParseError(ValueError):
    """Internal: a shorthand value could not be parsed (wrapped into ValidationError)."""


def parse_map_option(value: str, *, name: str, operation: str) -> dict[str, str]:
    """Parse ``k1=v1,k2=v2`` shorthand or a JSON object into a string map.

    A value whose leading non-whitespace character is ``{`` or ``[`` is read
    as JSON (aws's short-circuit to its JSON unpacker), and only an object
    whose values are all strings is accepted. Everything else is parsed with
    `_Parser`; a value loaded as bytes through ``@=fileb://`` is then refused,
    since a map value must be text. Every refusal is the ``Error parsing
    parameter '<name>'`` usage error (rc 252 via ``ValidationError``), a
    paramfile the ``@=`` operator cannot *load* included. aws reports that
    one load failure at 255 instead - its shorthand parser calls
    ``get_paramfile`` outside the handler that names the argument - an
    accident of its call path that is not copied: a file named on the
    command line that cannot be read is a command-line error wherever it is
    named.
    """
    text = value.strip()
    if text.startswith(("{", "[")):
        return _parse_json_map(value, name=name, operation=operation)
    try:
        with paramfile.named_argument(name, operation=operation):
            parsed = _Parser(value, operation=operation).parse()
    except _ShorthandParseError as exc:
        raise ValidationError(usage.param_error(name, exc), operation=operation) from exc
    for key, item in parsed.items():
        if isinstance(item, bytes):
            raise ValidationError(
                usage.param_error(
                    name, f"the value of key {key!r} must be text, not the bytes of a fileb:// file"
                ),
                operation=operation,
            )
    return cast("dict[str, str]", parsed)


def _parse_json_map(value: str, *, name: str, operation: str) -> dict[str, str]:
    """aws's map branch of ``_unpack_complex_cli_arg``: a JSON object of strings.

    A ``[`` lead is refused without decoding it: no array can be a map.
    """
    if value.lstrip()[0] != "{":
        raise ValidationError(
            usage.param_error(name, f"expected a JSON object, received: {value}"),
            operation=operation,
        )
    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            usage.param_error(name, f"Invalid JSON: {exc}\nJSON received: {value}"),
            operation=operation,
        ) from exc
    entries = cast("dict[str, object]", data)  # a `{`-led document decodes to an object
    for key, item in entries.items():
        if not isinstance(item, str):
            raise ValidationError(
                usage.param_error(
                    name, f"the value of key {key!r} must be a string, not {json.dumps(item)}"
                ),
                operation=operation,
            )
    return cast("dict[str, str]", entries)


class _Parser:
    """Recursive-descent port of aws-cli's ``ShorthandParser``, scalars only.

    ``parameter = keyval *("," keyval)`` over ``keyval = key ("=" / "@=")
    value``. aws's grammar goes on to build lists and nested maps, which no
    map of strings can hold; here a value that would start one is refused
    where it starts (`_values`, `_csv_value`). What is accepted is unchanged:
    the csv second-value probe that tells a list from the next pair is kept,
    since it is what absorbs ``a=b,,c=d``.

    The returned dict preserves insertion order, so a caller can keep the
    user's pair order.
    """

    def __init__(self, value: str, *, operation: str) -> None:
        self._value = value
        self._index = 0
        self._operation = operation
        self._resolve_paramfiles = False

    def parse(self) -> dict[str, _ShorthandValue]:
        """Parse the complete map, rejecting duplicate keys."""
        params: dict[str, _ShorthandValue] = {}
        key, val = self._keyval()
        params[key] = val
        last_index = self._index
        while self._index < len(self._value):
            self._expect(",", consume_whitespace=True)
            key, val = self._keyval()
            if key in params:
                raise _ShorthandParseError(
                    f'Second instance of key "{key}" encountered for input:\n'
                    f"{self._error_marker(last_index + 1)}\n"
                    'This is often because there is a preceding "," instead of a space.'
                )
            params[key] = val
            last_index = self._index
        return params

    def _keyval(self) -> tuple[str, _ShorthandValue]:
        """Parse one key/value pair, arming the optional `@=` paramfile operator."""
        # No empty-key guard - aws-cli's _keyval has none: with the cursor on
        # "=" the key is "" ("=bar" parses to {"": "bar"} and the transfer
        # proceeds, rc 0), and anything else fails _expect (rc 252).
        key = self._key()
        # aws grammar: keyval = key "=" [values] / key "@=" [file-optional-values].
        # '@' opts the value into paramfile resolution; aws probes it with the
        # same try/expect shape (whitespace consumed either way).
        self._resolve_paramfiles = False
        try:
            self._expect("@", consume_whitespace=True)
            self._resolve_paramfiles = True
        except _ShorthandParseError:
            pass
        self._expect("=", consume_whitespace=True)
        return key, self._values()

    def _key(self) -> str:
        start = self._index
        while not self._at_eof() and self._value[self._index] in _KEY_CHARS:
            self._index += 1
        return self._value[start : self._index]

    def _values(self) -> _ShorthandValue:
        """aws's ``values`` dispatch, without its list and hash-literal branches."""
        if self._at_eof():
            return ""
        char = self._value[self._index]
        if char in "[{":
            # aws's explicit list / hash literal: never a string.
            raise _ShorthandParseError(
                f"A value starting with '{char}' must be quoted for input:\n "
                f"{self._error_marker(self._index)}"
            )
        return self._csv_value()

    def _csv_value(self) -> _ShorthandValue:
        """aws's ``_csv_value``: one scalar, refusing the list that may follow it.

        After a ',' aws probes for a second value; the probe failing on
        anything but EOF means the ',' ended the pair, and aws backtracks only
        to the nearest ',' - which absorbs an empty segment between pairs
        (``a=b,,c=d`` is two pairs) and can land inside an escaped comma
        (``k=,\\,a=b``). An at-EOF failure propagates instead, which is why
        ``a=b,`` is a parse error. A probe that succeeds would make a csv
        list, which a map of strings cannot hold.
        """
        first_value = self._first_value()
        self._consume_whitespace()
        if self._at_eof() or self._value[self._index] != ",":
            return first_value
        self._expect(",", consume_whitespace=True)
        list_start = self._index
        try:
            self._second_value()
            self._consume_whitespace()
            if not self._at_eof():
                self._expect(",", consume_whitespace=True)
        except _ShorthandParseError:
            if self._at_eof():
                raise
            self._backtrack_to(",")
            return first_value
        raise _ShorthandParseError(
            "A value cannot be a list; write a literal comma as \\, or quote "
            f"the value, for input:\n {self._error_marker(list_start)}"
        )

    def _first_value(self) -> str | bytes:
        if self._current() == "'":
            return self._quoted_value(_SINGLE_QUOTED_RE, escaped_char="'", name="single quoted")
        if self._current() == '"':
            return self._quoted_value(_DOUBLE_QUOTED_RE, escaped_char='"', name="double quoted")
        match = _FIRST_VALUE_RE.match(self._value[self._index :])
        if match is None:
            # aws returns the empty value unresolved, without consuming anything.
            return ""
        return self._resolve(self._consume_match(match).replace("\\,", ",").rstrip())

    def _second_value(self) -> str | bytes:
        """A csv continuation value: `_first_value`'s shapes, narrower follow set.

        Unlike `_first_value` an unquoted miss is an error rather than an empty
        value - that error is what ends the csv list and triggers the backtrack.
        """
        if self._current() == "'":
            return self._quoted_value(_SINGLE_QUOTED_RE, escaped_char="'", name="single quoted")
        if self._current() == '"':
            return self._quoted_value(_DOUBLE_QUOTED_RE, escaped_char='"', name="double quoted")
        consumed = self._must_consume(_SECOND_VALUE_RE, name="second")
        return self._resolve(consumed.replace("\\,", ",").rstrip())

    def _quoted_value(self, regex: re.Pattern[str], *, escaped_char: str, name: str) -> str | bytes:
        """Consume one quoted scalar using the named aws-cli grammar fragment."""
        body = self._must_consume(regex, name=name)[1:-1]
        body = body.replace("\\" + escaped_char, escaped_char)
        return self._resolve(body.replace("\\\\", "\\"))

    def _resolve(self, value: str) -> str | bytes:
        """aws's ``_resolve_paramfiles``: load the value when ``@=`` armed the pair.

        A load failure is reported as the option's parse error by
        `parse_map_option`'s `paramfile.named_argument` scope.
        """
        if not self._resolve_paramfiles:
            return value
        loaded = paramfile.get_paramfile(value, operation=self._operation)
        return value if loaded is None else loaded

    def _must_consume(self, regex: re.Pattern[str], *, name: str) -> str:
        match = regex.match(self._value[self._index :])
        if match is None:
            raise _ShorthandParseError(
                f"Expected: '<{name}>', received: '<none>' for input:\n "
                f"{self._error_marker(self._index)}"
            )
        return self._consume_match(match)

    def _consume_match(self, match: re.Match[str]) -> str:
        start, end = match.span()
        consumed = self._value[self._index + start : self._index + end]
        self._index += end - start
        return consumed

    def _backtrack_to(self, char: str) -> None:
        while self._index >= 0 and self._value[self._index] != char:
            self._index -= 1

    def _expect(self, char: str, *, consume_whitespace: bool = False) -> None:
        """Consume an expected delimiter or raise a caret-positioned parse error."""
        if consume_whitespace:
            self._consume_whitespace()
        if self._at_eof():
            raise _ShorthandParseError(
                f"Expected: '{char}', received: 'EOF' for input:\n "
                f"{self._error_marker(self._index)}"
            )
        actual = self._value[self._index]
        if actual != char:
            raise _ShorthandParseError(
                f"Expected: '{char}', received: '{actual}' for input:\n "
                f"{self._error_marker(self._index)}"
            )
        self._index += 1
        if consume_whitespace:
            self._consume_whitespace()

    def _consume_whitespace(self) -> None:
        while not self._at_eof() and self._value[self._index] in string.whitespace:
            self._index += 1

    def _current(self) -> str | None:
        """The character under the cursor, or None at EOF (aws's ``_EOF`` sentinel)."""
        if self._index < len(self._value):
            return self._value[self._index]
        return None

    def _at_eof(self) -> bool:
        return self._index >= len(self._value)

    def _error_marker(self, index: int) -> str:
        # aws-cli ShorthandParseError._error_location: place the caret under the
        # offending column. A shell can embed newlines in an argument, so count
        # the column from the last newline before `index` and split the value
        # into consumed / remaining around the next newline after it.
        value = self._value
        consumed, remaining, num_spaces = value, "", index
        if "\n" in value[:index]:
            num_spaces = index - value[:index].rindex("\n") - 1
        if "\n" in value[index:]:
            next_newline = index + value[index:].index("\n")
            consumed, remaining = value[:next_newline], value[next_newline:]
        return f"{consumed}\n{' ' * num_spaces}^{remaining}"


__all__ = ["parse_map_option"]
