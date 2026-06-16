"""Parsing for aws-cli "map" option values (``--metadata KeyName1=string,...``).

aws-cli parses map-typed options with its shorthand grammar (``awscli/
shorthand.py`` ``ShorthandParser``) and also accepts the equivalent JSON object.
This module ports the subset those options actually
use - flat ``key=value`` pairs - faithfully, so the CLI matches aws on the
corner cases the naive ``split(",")``/``partition("=")`` got wrong:

- a duplicate key is rejected (``Second instance of key ...`` -> rc 252), not
  silently last-write-wins,
- backslash-escaped commas in an unquoted value (``k=a\\,b`` -> ``a,b``),
- single/double-quoted values that contain commas (``k="a,b"`` -> ``a,b``).

Error wording is shaped on aws's parser message (``Error parsing parameter
'<name>'``) closely enough for the stderr token contract; every parse failure
maps to aws's usage rc (252) via :class:`ValidationError`.
"""

from __future__ import annotations

import json
import re
import string

from boto3_s3 import ValidationError

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
_SINGLE_QUOTED_RE = re.compile(r"'(?:\\'|[^'])*'", re.UNICODE)
_DOUBLE_QUOTED_RE = re.compile(r'"(?:\\"|[^"])*"', re.UNICODE)


class _ShorthandParseError(ValueError):
    """Internal: a shorthand value could not be parsed (wrapped into ValidationError)."""


def parse_map_option(value: str, *, name: str, operation: str) -> dict[str, str]:
    """Parse ``k1=v1,k2=v2`` shorthand or a JSON object into a string map.

    Raises ``ValidationError`` (aws rc 252) with an ``Error parsing parameter
    '<name>'`` message on malformed input, mirroring aws's parser error class
    and wording closely enough for the stderr token contract.
    """
    text = value.strip()
    if text.startswith("{"):
        return _parse_json_map(value, name=name, operation=operation)
    try:
        return _Parser(value).parse()
    except _ShorthandParseError as exc:
        raise ValidationError(
            f"Error parsing parameter '{name}': {exc}", operation=operation
        ) from exc


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

    Scoped to flat scalar maps (the only shape ``--metadata`` uses): the
    explicit-list ``[...]`` / hash ``{...}`` constructs and the ``@=`` paramfile
    operator are not handled. The returned dict preserves insertion order, so a
    caller can keep the user's pair order.
    """

    def __init__(self, value: str) -> None:
        self._value = value
        self._index = 0

    def parse(self) -> dict[str, str]:
        params: dict[str, str] = {}
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

    def _keyval(self) -> tuple[str, str]:
        key = self._key()
        if not key:
            raise _ShorthandParseError(
                f"Expected: '<key>', received: '{self._current_for_msg()}' for input:\n"
                f"{self._error_marker(self._index)}"
            )
        self._expect("=", consume_whitespace=True)
        return key, self._scalar_value()

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
            return self._consume_quoted(_SINGLE_QUOTED_RE, escaped_char="'")
        if first == '"':
            return self._consume_quoted(_DOUBLE_QUOTED_RE, escaped_char='"')
        return self._first_value_unquoted()

    def _first_value_unquoted(self) -> str:
        match = _FIRST_VALUE_RE.match(self._value[self._index :])
        if match is None:
            return ""
        consumed = match.group(0)
        self._index += len(consumed)
        return consumed.replace("\\,", ",").rstrip()

    def _consume_quoted(self, regex: re.Pattern[str], *, escaped_char: str) -> str:
        match = regex.match(self._value[self._index :])
        if match is None:
            raise _ShorthandParseError(
                f"Expected: closing {escaped_char!r}, received: '<none>' for input:\n"
                f"{self._error_marker(self._index)}"
            )
        consumed = match.group(0)
        self._index += len(consumed)
        body = consumed[1:-1]
        body = body.replace("\\" + escaped_char, escaped_char)
        return body.replace("\\\\", "\\")

    def _expect(self, char: str, *, consume_whitespace: bool = False) -> None:
        if consume_whitespace:
            self._consume_whitespace()
        if self._at_eof():
            raise _ShorthandParseError(
                f"Expected: {char!r}, received: 'EOF' for input:\n{self._error_marker(self._index)}"
            )
        actual = self._value[self._index]
        if actual != char:
            raise _ShorthandParseError(
                f"Expected: {char!r}, received: {actual!r} for input:\n"
                f"{self._error_marker(self._index)}"
            )
        self._index += 1
        if consume_whitespace:
            self._consume_whitespace()

    def _consume_whitespace(self) -> None:
        while not self._at_eof() and self._value[self._index] in string.whitespace:
            self._index += 1

    def _current_for_msg(self) -> str:
        return self._value[self._index] if self._index < len(self._value) else "EOF"

    def _at_eof(self) -> bool:
        return self._index >= len(self._value)

    def _error_marker(self, index: int) -> str:
        # aws-cli caret-line layout for a single-line input (shorthand CLI tokens
        # carry no newlines, so the multi-line variant is not reproduced).
        return f"{self._value}\n{' ' * index}^"


__all__ = ["parse_map_option"]
