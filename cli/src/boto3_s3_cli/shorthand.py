"""Parsing for aws-cli "map" option values (``--metadata KeyName1=string,...``).

aws-cli parses map-typed options with its shorthand grammar (``awscli/
shorthand.py`` ``ShorthandParser``) and also accepts the equivalent JSON object.
This module ports the subset those options actually
use - flat ``key=value`` pairs - faithfully, so the CLI matches aws on the
corner cases the naive ``split(",")``/``partition("=")`` got wrong:

- a duplicate key is rejected (``Second instance of key ...`` -> rc 252), not
  silently last-write-wins,
- backslash-escaped commas in an unquoted value (``k=a\\,b`` -> ``a,b``),
- single/double-quoted values that contain commas (``k="a,b"`` -> ``a,b``),
- the ``@=`` paramfile operator (``k@=file://path`` loads the file's text,
  ``fileb://`` its bytes; a prefix-less value passes through - aws's
  "file-optional-values").

Error wording is shaped on aws's parser message (``Error parsing parameter
'<name>'``) closely enough for the stderr token contract; every parse failure
maps to aws's usage rc (252) via ``ValidationError``.
"""

from __future__ import annotations

import json
import re
import string

from boto3_s3 import ValidationError
from boto3_s3_cli import paramfile

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
# the probe in `_try_csv_continuation` fail over to the next pair.
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

    Raises ``ValidationError`` (aws rc 252) with an ``Error parsing parameter
    '<name>'`` message on malformed input, mirroring aws's parser error class
    and wording closely enough for the stderr token contract. A ``bytes``
    value (only reachable via the ``@=`` operator's ``fileb://`` load) is
    rejected here too: aws schema-validates the shorthand result at parse
    time - a map value must be a string - so ``a@=fileb://...`` is its
    pre-pipeline ParamValidation (rc 252, measured), never a transfer.
    """
    text = value.strip()
    if text.startswith("{"):
        return _parse_json_map(value, name=name, operation=operation)
    try:
        parsed = _Parser(value, name=name, operation=operation).parse()
    except _ShorthandParseError as exc:
        raise ValidationError(
            f"Error parsing parameter '{name}': {exc}", operation=operation
        ) from exc
    result: dict[str, str] = {}
    for key, val in parsed.items():
        if isinstance(val, bytes):
            raise ValidationError(
                f"Error parsing parameter '{name}': Invalid type for parameter {key}, "
                f"value: {val!r}, valid types: <class 'str'>",
                operation=operation,
            )
        result[key] = val
    return result


def _parse_json_map(value: str, *, name: str, operation: str) -> dict[str, str]:
    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            f"Error parsing parameter '{name}': Invalid JSON: {exc}\nJSON received: {value}",
            operation=operation,
        ) from exc
    if not isinstance(data, dict) or not all(
        isinstance(key, str) and isinstance(item, str)
        for key, item in data.items()  # pyright: ignore[reportUnknownVariableType]
    ):
        raise ValidationError(
            f"Error parsing parameter '{name}': expected a JSON object of strings",
            operation=operation,
        )
    return data  # pyright: ignore[reportUnknownVariableType]


class _Parser:
    """Recursive-descent flat-map parser mirroring aws-cli's ``ShorthandParser``.

    Scoped to flat scalar maps plus the ``@=`` paramfile operator (the shapes
    ``--metadata`` accepts); the explicit-list ``[...]`` / hash ``{...}`` /
    csv-list (``a=b,c``) constructs are not handled. On those non-scalar
    shapes the rc still matches aws (252, measured), but the stderr wording
    differs: aws parses them and then fails its schema validation, while this
    parser raises a syntax error. The returned dict preserves insertion
    order, so a caller can keep the user's pair order.
    """

    def __init__(self, value: str, *, name: str, operation: str) -> None:
        self._value = value
        self._index = 0
        self._name = name
        self._operation = operation

    def parse(self) -> dict[str, str | bytes]:
        """Parse the complete flat map, rejecting duplicates with aws-cli wording."""
        params: dict[str, str | bytes] = {}
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

    def _keyval(self) -> tuple[str, str | bytes]:
        """Parse one key/value pair and resolve the optional `@=` paramfile form."""
        # No empty-key guard - aws-cli's _keyval has none: with the cursor on
        # "=" the key is "" ("=bar" parses to {"": "bar"} and the transfer
        # proceeds, rc 0), and anything else fails _expect with aws's
        # "Expected: '='" wording (rc 252 either way).
        key = self._key()
        # aws grammar: keyval = key "=" [values] / key "@=" [file-optional-values].
        # '@' opts the value into paramfile resolution; aws probes it with the
        # same try/expect shape (whitespace consumed either way).
        resolve_paramfiles = False
        try:
            self._expect("@", consume_whitespace=True)
            resolve_paramfiles = True
        except _ShorthandParseError:
            pass
        self._expect("=", consume_whitespace=True)
        value = self._scalar_value()
        if resolve_paramfiles:
            loaded = paramfile.get_paramfile(value, name=self._name, operation=self._operation)
            if loaded is not None:
                return key, loaded
        return key, value

    def _key(self) -> str:
        start = self._index
        while not self._at_eof() and self._value[self._index] in _KEY_CHARS:
            self._index += 1
        return self._value[start : self._index]

    def _scalar_value(self) -> str:
        # A comma after the value belongs to the next key=value pair (parse's
        # loop handles it), so a single scalar is read and returned.
        if self._at_eof():
            return ""
        first = self._value[self._index]
        if first == "'":
            # "singled quoted" reproduces aws-cli's _NamedRegex name verbatim
            # (typo included) so the unterminated-quote wording matches byte for byte.
            value = self._consume_quoted(_SINGLE_QUOTED_RE, escaped_char="'", name="singled quoted")
        elif first == '"':
            value = self._consume_quoted(_DOUBLE_QUOTED_RE, escaped_char='"', name="double quoted")
        else:
            value = self._first_value_unquoted()
        # aws's _csv_value consumes whitespace after the value before its
        # EOF/comma check, so a trailing space - or the newline a file://
        # paramfile keeps at its end - after a *quoted* value is accepted
        # (`--metadata "title='hello' "`). The unquoted regex already consumes
        # trailing whitespace (and rstrips it), making this a no-op there.
        self._consume_whitespace()
        if not self._at_eof() and self._value[self._index] == ",":
            self._try_csv_continuation()
        return value

    def _try_csv_continuation(self) -> None:
        """aws's ``_csv_value`` second-value probe, scoped to the flat map.

        aws never returns a scalar without probing a following ',' for a csv
        list: the ',' is consumed and a second value attempted. The probe is
        observable even though we accept no lists - on failure aws backtracks
        only to the nearest ',', which absorbs an empty segment between pairs
        (``a=b,,c=d`` parses to two pairs; measured rc 0 on the pinned aws
        where the plain pair loop errored 252), and an at-EOF failure
        propagates (``a=b,`` is aws's parse error, '<second>' wording). A
        second value that *does* parse would build a csv list, schema-invalid
        for a flat string map: rewind to the original comma and let the pair
        loop re-read from there (the rc-252 shapes of the class docstring's
        scope note).
        """
        start = self._index  # at the ',' after the scalar
        self._expect(",", consume_whitespace=True)
        try:
            self._second_value()
        except _ShorthandParseError:
            if self._at_eof():
                raise
            self._backtrack_to(",")
            return
        # Second value parsed. aws now expects ',' to extend the csv list:
        # - next non-ws char is NOT ',': aws's _expect failure backtracks from
        #   *here* to the nearest ',' and keeps the scalar - landing inside an
        #   escaped comma when that is what the probe consumed (the
        #   ``k=,\\,a=b`` quirk, differentially verified against aws's parser);
        # - otherwise a csv list forms, schema-invalid for the flat string
        #   map: rewind to the original comma and let the pair loop report it
        #   (rc 252 like aws's schema rejection, wording per the scope note).
        self._consume_whitespace()
        if not self._at_eof() and self._value[self._index] != ",":
            self._backtrack_to(",")
            return
        self._index = start

    def _second_value(self) -> None:
        """Probe one csv second value (aws's ``_second_value``); result unused."""
        if not self._at_eof():
            first = self._value[self._index]
            if first == "'":
                self._consume_quoted(_SINGLE_QUOTED_RE, escaped_char="'", name="singled quoted")
                return
            if first == '"':
                self._consume_quoted(_DOUBLE_QUOTED_RE, escaped_char='"', name="double quoted")
                return
        match = _SECOND_VALUE_RE.match(self._value[self._index :])
        if match is None:
            # aws's _must_consume_regex wording for the 'second' fragment.
            raise _ShorthandParseError(
                f"Expected: '<second>', received: '<none>' for input:\n "
                f"{self._error_marker(self._index)}"
            )
        self._index += len(match.group(0))

    def _backtrack_to(self, char: str) -> None:
        while self._index >= 0 and self._value[self._index] != char:
            self._index -= 1

    def _first_value_unquoted(self) -> str:
        match = _FIRST_VALUE_RE.match(self._value[self._index :])
        if match is None:
            return ""
        consumed = match.group(0)
        self._index += len(consumed)
        return consumed.replace("\\,", ",").rstrip()

    def _consume_quoted(self, regex: re.Pattern[str], *, escaped_char: str, name: str) -> str:
        """Consume one quoted scalar using the named aws-cli grammar fragment."""
        match = regex.match(self._value[self._index :])
        if match is None:
            raise _ShorthandParseError(
                f"Expected: '<{name}>', received: '<none>' for input:\n "
                f"{self._error_marker(self._index)}"
            )
        consumed = match.group(0)
        self._index += len(consumed)
        body = consumed[1:-1]
        body = body.replace("\\" + escaped_char, escaped_char)
        return body.replace("\\\\", "\\")

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
