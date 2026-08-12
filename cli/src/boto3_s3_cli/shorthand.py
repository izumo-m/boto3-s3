"""Parsing for aws-cli "map" option values (``--metadata KeyName1=string,...``).

aws-cli parses map-typed options with its shorthand grammar (``awscli/
shorthand.py`` ``ShorthandParser``) and also accepts the equivalent JSON object.
`_Parser` is a port of that grammar - the whole of it, csv lists, explicit
lists and hash literals included - so the CLI matches aws on the corner cases
the naive ``split(",")``/``partition("=")`` got wrong:

- a duplicate key is rejected (``Second instance of key ...`` -> rc 252), not
  silently last-write-wins,
- backslash-escaped commas in an unquoted value (``k=a\\,b`` -> ``a,b``),
- single/double-quoted values that contain commas (``k="a,b"`` -> ``a,b``),
- the ``@=`` paramfile operator (``k@=file://path`` loads the file's text,
  ``fileb://`` its bytes; a prefix-less value passes through - aws's
  "file-optional-values"),
- the non-scalar shapes (``k=a,b``, ``k=[a,b]``, ``k={a=b}``) parse and are
  then rejected by the *schema*, not by the grammar - a map of strings cannot
  hold a list or a nested map, so they draw botocore's ``Parameter validation
  failed`` report rather than a syntax error.

Two error vocabularies therefore reach the user, exactly as in aws. A grammar
failure is the parser's own ``Error parsing parameter '<name>'`` (rc 252 via
``ValidationError``); a well-formed value of the wrong type is botocore's
schema report, which carries no option name at all. A failure to *load* an
``@=`` paramfile is neither: it leaves this module unwrapped and exits 255
(`paramfile.named_argument`).
"""

from __future__ import annotations

import json
import re
import string
from typing import TypeAlias, cast

from boto3_s3 import ValidationError
from boto3_s3_cli import paramfile

# What one shorthand key can carry: a scalar, a `fileb://` load, or - through
# the csv / explicit-list / hash-literal forms - a nesting of those.
_ShorthandValue: TypeAlias = "str | bytes | list[_ShorthandValue] | dict[str, _ShorthandValue]"

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

# json's own whitespace set (json.decoder.WHITESPACE_STR).
_JSON_WHITESPACE = " \t\n\r"


class _ShorthandParseError(ValueError):
    """Internal: a shorthand value could not be parsed (wrapped into ValidationError)."""


def parse_map_option(value: str, *, name: str, operation: str) -> dict[str, str]:
    """Parse ``k1=v1,k2=v2`` shorthand or a JSON object into a string map.

    A value whose leading non-whitespace character is ``{`` or ``[`` never
    reaches the shorthand grammar: aws short-circuits both to its JSON
    unpacker, which then insists on ``{`` and rejects a ``[`` lead with a bare
    ``Invalid JSON:`` echo of the value.

    Everything else is parsed with `_Parser`, and the result is schema-checked
    the way aws checks it afterwards - a map value must be a string. So a
    ``bytes`` (from ``@=fileb://``), a csv or explicit list, and a hash literal
    all raise botocore's ``Parameter validation failed`` report, one line per
    offending key in the map's own order, with no option name in it. Grammar
    failures raise the parser's ``Error parsing parameter '<name>'`` instead.
    Both are aws's usage rc (252) via ``ValidationError``. A paramfile the
    ``@=`` operator cannot *load* is not this function's error at all - see
    `_Parser._resolve`.
    """
    text = value.strip()
    if text.startswith(("{", "[")):
        return _parse_json_map(value, name=name, operation=operation)
    try:
        parsed = _Parser(value, operation=operation).parse()
    except _ShorthandParseError as exc:
        raise ValidationError(
            f"Error parsing parameter '{name}': {exc}", operation=operation
        ) from exc
    invalid = {key: item for key, item in parsed.items() if not isinstance(item, str)}
    if invalid:
        # botocore renders the offending value with str(), which for every
        # non-string shape the grammar can build (bytes, list, dict) is its
        # repr; unlike the JSON route there is no OrderedDict to hand-format.
        raise ValidationError(
            "Parameter validation failed:\n"
            + "\n".join(
                f"Invalid type for parameter {key}, value: {item!r}, "
                f"type: {type(item)}, valid types: <class 'str'>"
                for key, item in invalid.items()
            ),
            operation=operation,
        )
    return cast("dict[str, str]", parsed)


def _parse_json_map(value: str, *, name: str, operation: str) -> dict[str, str]:
    """aws's map branch of ``_unpack_complex_cli_arg``: JSON object, or nothing.

    The branch is reached for a ``[`` lead as well as a ``{`` one, and answers
    the ``[`` with an ``Invalid JSON:`` line followed by the value verbatim -
    not lstripped, and with none of the decoder detail or ``JSON received:``
    line a real decode failure carries.
    """
    if value.lstrip()[0] != "{":
        raise ValidationError(
            f"Error parsing parameter '{name}': Invalid JSON:\n{value}", operation=operation
        )
    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            f"Error parsing parameter '{name}': Invalid JSON: {_json_decode_message(exc)}\n"
            f"JSON received: {value}",
            operation=operation,
        ) from exc
    entries = cast("dict[str, object]", data)  # a `{`-led document decodes to an object
    invalid = {key: item for key, item in entries.items() if not isinstance(item, str)}
    if invalid:
        # botocore's schema report, like the one in `parse_map_option`: aws
        # parses the JSON and then fails its schema validation with one line
        # per offending key, in document order.
        reports = "\n".join(
            f"Invalid type for parameter {key}, value: {_json_value_repr(item)}, "
            f"type: {_json_type_repr(item)}, valid types: <class 'str'>"
            for key, item in invalid.items()
        )
        raise ValidationError("Parameter validation failed:\n" + reports, operation=operation)
    return cast("dict[str, str]", entries)


def _json_decode_message(exc: json.JSONDecodeError) -> str:
    """Render a decode failure the way the official aws build's json does.

    Python 3.13 gave the two trailing-comma shapes a message of their own,
    anchored on the comma instead of on the closing bracket. aws ships 3.14,
    so the newer text is the parity target on every interpreter this CLI runs
    on; the older ones are re-anchored here. Those two shapes are the whole
    difference (measured across 3.10 / 3.12 / 3.14 over a broken-JSON corpus).
    """
    if exc.msg == "Expecting property name enclosed in double quotes":
        closer, message = "}", "Illegal trailing comma before end of object"
    elif exc.msg == "Expecting value":
        closer, message = "]", "Illegal trailing comma before end of array"
    else:
        return str(exc)
    doc = exc.doc
    if exc.pos >= len(doc) or doc[exc.pos] != closer:
        return str(exc)
    comma = exc.pos - 1
    while comma >= 0 and doc[comma] in _JSON_WHITESPACE:
        comma -= 1
    if comma < 0 or doc[comma] != ",":
        return str(exc)
    lineno = doc.count("\n", 0, comma) + 1
    colno = comma - doc.rfind("\n", 0, comma)
    return f"{message}: line {lineno} column {colno} (char {comma})"


def _json_value_repr(value: object) -> str:
    """Render a JSON-decoded value the way aws's schema report prints it.

    aws parses the JSON with ``object_pairs_hook=OrderedDict``, so an object
    value prints in ``OrderedDict`` form. Formatting that by hand - rather
    than parsing into ``OrderedDict`` and calling ``repr`` - keeps the text
    identical across host interpreters: ``OrderedDict.__repr__`` changed
    form in Python 3.12, and aws's official build ships the new one.
    """
    if isinstance(value, dict):
        mapping = cast("dict[object, object]", value)
        if not mapping:
            return "OrderedDict()"
        items = ", ".join(f"{key!r}: {_json_value_repr(item)}" for key, item in mapping.items())
        return "OrderedDict({" + items + "})"
    if isinstance(value, list):
        elements = cast("list[object]", value)
        return "[" + ", ".join(_json_value_repr(item) for item in elements) + "]"
    return repr(value)


def _json_type_repr(value: object) -> str:
    if isinstance(value, dict):
        return "<class 'collections.OrderedDict'>"
    return str(type(value))


class _Parser:
    """Recursive-descent port of aws-cli's ``ShorthandParser``.

    The whole grammar is here - ``parameter = keyval *("," keyval)`` over
    ``keyval = key ("=" / "@=") values`` and ``values = csv-list /
    explicit-list / hash-literal`` - because aws's diagnostics depend on it:
    a value the grammar accepts but the map's schema cannot hold is reported
    as a type error, and only a value the grammar rejects is a syntax error.
    Narrowing the grammar to flat scalars would move that boundary.

    The returned dict preserves insertion order, so a caller can keep the
    user's pair order; nested hash literals do the same and, like aws, accept
    a repeated key by overwriting (only the top level rejects duplicates).
    """

    def __init__(self, value: str, *, operation: str) -> None:
        self._value = value
        self._index = 0
        self._operation = operation
        self._resolve_paramfiles = False

    def parse(self) -> dict[str, _ShorthandValue]:
        """Parse the complete map, rejecting duplicate keys with aws-cli wording."""
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
        # proceeds, rc 0), and anything else fails _expect with aws's
        # "Expected: '='" wording (rc 252 either way).
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
        """aws's ``values = csv-list / explicit-list / hash-literal`` dispatch."""
        if self._at_eof():
            return ""
        char = self._value[self._index]
        if char == "[":
            return self._explicit_list()
        if char == "{":
            return self._hash_literal()
        return self._csv_value()

    def _csv_value(self) -> _ShorthandValue:
        """aws's ``_csv_value``: one scalar, or the comma-separated list after it.

        The second-value probe is observable even when no list survives the
        schema. On failure aws backtracks only to the nearest ',', which
        absorbs an empty segment between pairs (``a=b,,c=d`` is two pairs) and
        can land inside an escaped comma (``k=,\\,a=b``); an at-EOF failure
        propagates instead, which is why ``a=b,`` is a parse error.
        """
        first_value = self._first_value()
        self._consume_whitespace()
        if self._at_eof() or self._value[self._index] != ",":
            return first_value
        self._expect(",", consume_whitespace=True)
        csv_list: list[_ShorthandValue] = [first_value]
        while True:
            try:
                current = self._second_value()
                self._consume_whitespace()
                if self._at_eof():
                    csv_list.append(current)
                    break
                self._expect(",", consume_whitespace=True)
                csv_list.append(current)
            except _ShorthandParseError:
                if self._at_eof():
                    raise
                self._backtrack_to(",")
                break
        if len(csv_list) == 1:
            return first_value
        return csv_list

    def _explicit_list(self) -> list[_ShorthandValue]:
        """aws's ``explicit-list``: ``"[" [value *("," value)] "]"``.

        The loop re-checks for the closer after every separator, so a trailing
        comma before ``]`` is accepted (``k=[a,]`` is ``['a']``).
        """
        self._expect("[", consume_whitespace=True)
        values: list[_ShorthandValue] = []
        while self._current() != "]":
            values.append(self._explicit_values())
            self._consume_whitespace()
            if self._current() != "]":
                self._expect(",")
                self._consume_whitespace()
        self._expect("]")
        return values

    def _explicit_values(self) -> _ShorthandValue:
        char = self._current()
        if char == "[":
            return self._explicit_list()
        if char == "{":
            return self._hash_literal()
        return self._first_value()

    def _hash_literal(self) -> dict[str, _ShorthandValue]:
        """aws's ``hash-literal``: ``"{" key ("=" / "@=") value ... "}"``.

        The ``@=`` operator is re-armed per inner key, so an outer ``@=`` does
        not reach the nested values, and a repeated inner key overwrites
        rather than raising the top level's duplicate error.
        """
        self._expect("{", consume_whitespace=True)
        keyvals: dict[str, _ShorthandValue] = {}
        while self._current() != "}":
            key = self._key()
            self._resolve_paramfiles = False
            try:
                self._expect("@", consume_whitespace=True)
                self._resolve_paramfiles = True
            except _ShorthandParseError:
                pass
            self._expect("=", consume_whitespace=True)
            value = self._explicit_values()
            self._consume_whitespace()
            if self._current() != "}":
                self._expect(",")
                self._consume_whitespace()
            keyvals[key] = value
        self._expect("}")
        return keyvals

    def _first_value(self) -> str | bytes:
        if self._current() == "'":
            # "singled quoted" reproduces aws-cli's _NamedRegex name verbatim
            # (typo included) so the unterminated-quote wording matches byte for byte.
            return self._quoted_value(_SINGLE_QUOTED_RE, escaped_char="'", name="singled quoted")
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
            return self._quoted_value(_SINGLE_QUOTED_RE, escaped_char="'", name="singled quoted")
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

        Outside `paramfile.named_argument` on purpose: aws's shorthand parser
        calls `get_paramfile` itself, so a load failure here is not the
        option's parse error but a bare `ResourceLoadingError` reaching its
        general handler - rc 255 without the ParamValidation envelope
        (measured: ``--metadata k@=file:///no/x``).
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
