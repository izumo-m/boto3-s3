"""``boto3_s3.awsconfig``: a general reader for the AWS config file (``~/.aws/config``).

This is the building block behind :meth:`boto3_s3.S3.aws_config`. It reads the
AWS **config file** - ``AWS_CONFIG_FILE`` (default ``~/.aws/config``) - and lets
an application pull any value out of it: not just the ``[s3]`` transfer tuning,
but ``region`` / ``output`` / a ``[services ...]`` block / an ``[sso-session
...]`` block / ``[plugins]`` - anything the file holds.

The parse itself is **delegated to botocore** (``Session.full_config`` /
``Session.get_scoped_config``) rather than reinventing INI handling, so the file
location, the nested ``s3 =`` subsection syntax, and the active-profile
resolution all behave exactly like ``aws`` / ``boto3``. botocore keeps every
value as a **string** (with one level of subsection nesting), validates nothing,
and preserves unknown sections and keys - which is precisely the lazy,
string-first model this reader exposes.

The shape mirrors ``aws configure get``'s feel - the **profile is the default
context** and ``services`` / ``sso-session`` are explicit selectors - with two
deliberate additions ``aws``'s loader does not have:

- **typed getters** (:meth:`~ConfigSection.get_str` / ``get_int`` / ``get_size``
  / ``get_bool`` / ``get_rate``); ``aws configure get`` returns only strings;
- **caller-supplied defaults**; this reader holds no central defaults table.

A value is interpreted only when it is fetched: ``get_size("...chunksize")``
reads ``MB`` from ``"16MB"`` and returns bytes, while ``get_str`` returns
``"16MB"`` verbatim. Sizes/rates are 1024-based, matching ``aws s3`` so that
reading a value written for ``aws`` yields the same number.

Resolution rules:

- a **missing** key, or an explicitly named but absent section, returns the
  caller's default (tolerant);
- a **present** value that does not convert (``get_size("abc")``), or a key that
  points at a whole subsection instead of a value, raises
  :class:`~boto3_s3.exceptions.InvalidConfigError` (a config typo is surfaced,
  not silently defaulted);
- the **active profile** is resolved through botocore's ``get_scoped_config``: a
  ``--profile`` / ``AWS_PROFILE`` that is set but missing surfaces as an
  ``InvalidConfigError`` (botocore's ``ProfileNotFound``, converted at the
  boundary), matching ``aws``.

Like :mod:`~boto3_s3.etagcompare` this is a standalone, opt-in building block:
imported by submodule path (``from boto3_s3.awsconfig import AwsConfig``), **not**
part of the package's lazy root re-export, and SDK-free at import time - boto3 /
botocore are loaded lazily on the read path, so ``import boto3_s3.awsconfig``
stays free of the SDK tax (import contract, docs/imports.md). It reads the
**config file only**, never ``~/.aws/credentials`` (secrets).

The aws-cli ``[s3]`` *runtime config* (the ``DEFAULTS`` table, value validation,
the ``default`` -> ``classic`` alias, and the classic/CRT engine decision) is a
separate, higher-level concern that lives in the CLI distribution
(``boto3_s3_cli.runtimeconfig``); it is intentionally **not** folded into this
general reader. The ``[s3]`` values are still reachable here - e.g.
``cfg.profile().get_size("s3.multipart_chunksize", default)``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast, overload

from boto3_s3.exceptions import InvalidConfigError

if TYPE_CHECKING:
    import boto3

__all__ = ["AwsConfig", "ConfigSection"]

# aws-cli's human_readable_to_int suffix table (1024-based: "MB" == "MiB").
# Reading a value written for `aws s3` must yield the same byte count. This is
# the ONE home of the table and the suffix rule: the CLI's aws-verbatim
# human_readable_to_int (boto3_s3_cli.runtimeconfig) shares them, while each
# consumer keeps its own boundary guard and error wording - the library's
# _parse_size below is deliberately hardened where the CLI port stays
# aws-faithful (bare-"mb" edge included).
SIZE_SUFFIX = {
    "kb": 1024,
    "mb": 1024**2,
    "gb": 1024**3,
    "tb": 1024**4,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
}


def split_size_suffix(value: str) -> tuple[str, str]:
    """Lowercase *value* and split off the candidate size suffix.

    Returns ``(lowered, suffix)``: the suffix is the trailing three characters
    for an ``ib`` spelling (``"mib"``), else the trailing two (``"mb"``). No
    validation happens here - each caller checks the suffix against
    :data:`SIZE_SUFFIX` under its own boundary guard.
    """
    lowered = value.lower()
    suffix = lowered[-3:] if lowered.endswith("ib") else lowered[-2:]
    return lowered, suffix


# Sentinel for "key absent" - distinct from a present value (which is always a
# string, or a Mapping for a subsection), so `_lookup` never confuses a real
# value with a miss.
_MISSING: Any = object()


class ConfigSection:
    """A single resolved section of the config file, with typed getters.

    Wraps the string tree botocore parsed for one section (a profile, a
    ``[services ...]`` block, ...). A ``key`` may be **dotted** to reach into a
    nested subsection - ``"s3.multipart_chunksize"`` reads ``multipart_chunksize``
    from the section's ``s3`` block. A missing key returns the caller's default;
    a present value that does not convert, or a key naming a whole subsection,
    raises :class:`~boto3_s3.exceptions.InvalidConfigError`. Obtain one from
    :class:`AwsConfig` (``cfg.profile(...)`` / ``cfg.services(...)`` / ...).
    """

    __slots__ = ("_data",)

    def __init__(self, data: Mapping[str, Any]) -> None:
        self._data = data

    @overload
    def get_str(self, key: str, default: str) -> str: ...
    @overload
    def get_str(self, key: str, default: None = None) -> str | None: ...
    def get_str(self, key: str, default: str | None = None) -> str | None:
        """The raw string value at ``key`` (verbatim, no interpretation)."""
        value = self._lookup(key)
        if value is _MISSING:
            return default
        return _require_scalar(value, key)

    @overload
    def get_int(self, key: str, default: int) -> int: ...
    @overload
    def get_int(self, key: str, default: None = None) -> int | None: ...
    def get_int(self, key: str, default: int | None = None) -> int | None:
        """The value at ``key`` as a plain integer (no range validation)."""
        value = self._lookup(key)
        if value is _MISSING:
            return default
        text = _require_scalar(value, key)
        try:
            return int(text)
        except ValueError:
            raise InvalidConfigError(_not_a(key, text, "an integer")) from None

    @overload
    def get_size(self, key: str, default: int) -> int: ...
    @overload
    def get_size(self, key: str, default: None = None) -> int | None: ...
    def get_size(self, key: str, default: int | None = None) -> int | None:
        """The value at ``key`` as a byte count (``"16MB"`` -> ``16 * 1024**2``)."""
        value = self._lookup(key)
        if value is _MISSING:
            return default
        return _parse_size(_require_scalar(value, key), key)

    @overload
    def get_rate(self, key: str, default: int) -> int: ...
    @overload
    def get_rate(self, key: str, default: None = None) -> int | None: ...
    def get_rate(self, key: str, default: int | None = None) -> int | None:
        """The value at ``key`` as bytes/second (``"10MB/s"`` / ``"800Kb/s"``)."""
        value = self._lookup(key)
        if value is _MISSING:
            return default
        return _parse_rate(_require_scalar(value, key), key)

    @overload
    def get_bool(self, key: str, default: bool) -> bool: ...
    @overload
    def get_bool(self, key: str, default: None = None) -> bool | None: ...
    def get_bool(self, key: str, default: bool | None = None) -> bool | None:
        """The value at ``key`` as a boolean (botocore ``ensure_boolean``)."""
        value = self._lookup(key)
        if value is _MISSING:
            return default
        # Deferred botocore import: only the read path pays it (import contract).
        from botocore.utils import ensure_boolean

        return ensure_boolean(_require_scalar(value, key))

    def _lookup(self, key: str) -> Any:
        """Walk a dotted ``key`` through nested subsections; ``_MISSING`` if absent."""
        current: Any = self._data
        for part in key.split("."):
            if isinstance(current, Mapping) and part in current:
                current = cast("Mapping[str, Any]", current)[part]
            else:
                return _MISSING
        return current


class AwsConfig:
    """Reader for the AWS config file (``~/.aws/config``), bound to a session.

    Built from a boto3 session (or a default one) via :meth:`from_session`; the
    file is parsed by botocore and cached on first access. ``profile`` is the
    default context - the bare getters (:meth:`get_size` ...) read the session's
    **active** profile, mirroring ``aws configure get varname`` - while
    :meth:`services` / :meth:`sso_session` / :meth:`plugins` select other
    sections, like ``aws``'s ``--services`` / ``--sso-session``. Each selector
    returns a :class:`ConfigSection`.

    Reachable from :meth:`boto3_s3.S3.aws_config`, which memoizes one per ``S3``
    instance.
    """

    __slots__ = ("_active", "_full", "_session")

    def __init__(self, session: Any) -> None:
        # `session` is a botocore Session (duck-typed: `.full_config` /
        # `.get_scoped_config()`); typing it loosely keeps this module's import
        # SDK-free. Use `from_session` rather than constructing directly.
        self._session = session
        self._full: Mapping[str, Any] | None = None
        self._active: ConfigSection | None = None

    @classmethod
    def from_session(cls, session: boto3.Session | None = None) -> AwsConfig:
        """Build a reader from a boto3 session (or a default ``AWS_PROFILE``-aware one).

        ``None`` builds ``boto3.Session()`` - the same default/``AWS_PROFILE``
        resolution the zero-config ``S3()`` uses for its client.
        """
        # Deferred: importing boto3 drags in botocore + s3transfer (~100ms), so
        # only an actual config read pays it (import contract, docs/imports.md).
        import boto3
        from botocore.exceptions import BotoCoreError

        if session is None:
            # Building the default session resolves the active profile eagerly
            # (boto3 sets up its loader): a set-but-missing AWS_PROFILE raises
            # ProfileNotFound here, converted to the library taxonomy. A caller
            # who passes their own session has already constructed it, so any
            # such error surfaced at their call, not here.
            try:
                session = boto3.Session()
            except BotoCoreError as exc:
                raise InvalidConfigError(str(exc)) from exc
        # aws-cli reads the same botocore session surface (the scoped/full config
        # lives on the underlying botocore Session, not the boto3 wrapper).
        return cls(session._session)  # pyright: ignore[reportPrivateUsage]

    # -- section selectors ------------------------------------------------

    def profile(self, name: str | None = None) -> ConfigSection:
        """A profile section. ``None`` = the session's active profile.

        ``name=None`` resolves the active profile through botocore's
        ``get_scoped_config`` (``--profile`` / ``AWS_PROFILE``, else
        ``[default]``); a set-but-missing profile raises
        :class:`~boto3_s3.exceptions.InvalidConfigError`. A given ``name`` reads
        ``[profile name]`` (or ``[default]`` for ``"default"``) from the full
        config and is tolerant - an absent profile yields an empty section whose
        getters fall to their defaults.
        """
        if name is None:
            return self._active_profile()
        return ConfigSection(self._sub("profiles", name))

    def services(self, name: str) -> ConfigSection:
        """A ``[services name]`` section (tolerant: absent -> empty section)."""
        return ConfigSection(self._sub("services", name))

    def sso_session(self, name: str) -> ConfigSection:
        """An ``[sso-session name]`` section (tolerant: absent -> empty section)."""
        return ConfigSection(self._sub("sso_sessions", name))

    def plugins(self) -> ConfigSection:
        """The ``[plugins]`` section (tolerant: absent -> empty section)."""
        return ConfigSection(self._top("plugins"))

    # -- active-profile shortcuts (delegate to `profile(None)`) -----------

    @overload
    def get_str(self, key: str, default: str) -> str: ...
    @overload
    def get_str(self, key: str, default: None = None) -> str | None: ...
    def get_str(self, key: str, default: str | None = None) -> str | None:
        """:meth:`ConfigSection.get_str` on the active profile."""
        return self._active_profile().get_str(key, default)

    @overload
    def get_int(self, key: str, default: int) -> int: ...
    @overload
    def get_int(self, key: str, default: None = None) -> int | None: ...
    def get_int(self, key: str, default: int | None = None) -> int | None:
        """:meth:`ConfigSection.get_int` on the active profile."""
        return self._active_profile().get_int(key, default)

    @overload
    def get_size(self, key: str, default: int) -> int: ...
    @overload
    def get_size(self, key: str, default: None = None) -> int | None: ...
    def get_size(self, key: str, default: int | None = None) -> int | None:
        """:meth:`ConfigSection.get_size` on the active profile."""
        return self._active_profile().get_size(key, default)

    @overload
    def get_rate(self, key: str, default: int) -> int: ...
    @overload
    def get_rate(self, key: str, default: None = None) -> int | None: ...
    def get_rate(self, key: str, default: int | None = None) -> int | None:
        """:meth:`ConfigSection.get_rate` on the active profile."""
        return self._active_profile().get_rate(key, default)

    @overload
    def get_bool(self, key: str, default: bool) -> bool: ...
    @overload
    def get_bool(self, key: str, default: None = None) -> bool | None: ...
    def get_bool(self, key: str, default: bool | None = None) -> bool | None:
        """:meth:`ConfigSection.get_bool` on the active profile."""
        return self._active_profile().get_bool(key, default)

    # -- internals --------------------------------------------------------

    def _active_profile(self) -> ConfigSection:
        if self._active is None:
            self._active = ConfigSection(self._scoped())
        return self._active

    def _scoped(self) -> Mapping[str, Any]:
        # Deferred botocore import: only the read path reaches here.
        from botocore.exceptions import BotoCoreError

        try:
            return self._session.get_scoped_config()
        except BotoCoreError as exc:
            # ProfileNotFound / ConfigParseError / ... -> the library taxonomy
            # (exceptions.md: backend errors are converted at the boundary).
            raise InvalidConfigError(str(exc)) from exc

    def _full_config(self) -> Mapping[str, Any]:
        full = self._full
        if full is None:
            from botocore.exceptions import BotoCoreError

            try:
                full = self._session.full_config
            except BotoCoreError as exc:
                raise InvalidConfigError(str(exc)) from exc
            self._full = full
        return full

    def _sub(self, kind: str, name: str) -> Mapping[str, Any]:
        """A named section under one of full_config's sub-maps (profiles/...)."""
        return _as_section(_as_section(self._full_config().get(kind)).get(name))

    def _top(self, name: str) -> Mapping[str, Any]:
        """A top-level section of full_config (e.g. ``plugins``)."""
        return _as_section(self._full_config().get(name))


def _as_section(value: Any) -> Mapping[str, Any]:
    """A resolved value as a section mapping, or an empty one if it is not a section."""
    if isinstance(value, Mapping):
        return cast("Mapping[str, Any]", value)
    return {}


def _require_scalar(value: Any, key: str) -> str:
    """The string at a resolved value; raise if it is a whole subsection."""
    if isinstance(value, Mapping):
        raise InvalidConfigError(f"config key {key!r} refers to a section, not a value")
    return value if isinstance(value, str) else str(value)


def _not_a(key: str, value: str, expected: str) -> str:
    return f"config value for {key!r} is not {expected}: {value!r}"


def _parse_size(value: str, key: str) -> int:
    """Bytes from a human-readable size (``"16MB"`` -> ``16 * 1024**2``).

    1024-based, matching aws-cli's ``human_readable_to_int`` (``MB`` == ``MiB``).
    A bare integer string passes through; anything else raises.
    """
    lowered, suffix = split_size_suffix(value)
    if len(lowered) > len(suffix) and suffix in SIZE_SUFFIX:
        try:
            return int(lowered[: -len(suffix)]) * SIZE_SUFFIX[suffix]
        except ValueError:
            raise InvalidConfigError(_not_a(key, value, "a size")) from None
    try:
        return int(lowered)
    except ValueError:
        raise InvalidConfigError(_not_a(key, value, "a size")) from None


def _parse_rate(value: str, key: str) -> int:
    """Bytes/second from a rate (``"10MB/s"`` bytes, ``"800Kb/s"`` bits, or a bare int).

    Ports aws-cli's rate parsing: a ``B/s`` value is bytes/s, a ``b/s`` value is
    bits/s (divided by 8), a bare integer is bytes/s. Magnitudes use the same
    1024-based suffixes as :func:`_parse_size`.
    """
    if value.endswith("B/s"):
        return _rate_magnitude(value, key)
    if value.endswith("b/s"):
        return int(_rate_magnitude(value, key) / 8)
    try:
        return int(value)
    except ValueError:
        raise InvalidConfigError(_not_a(key, value, "a rate")) from None


def _rate_magnitude(value: str, key: str) -> int:
    # "1024B/s" has no magnitude prefix -> strip "B/s" and read the integer;
    # "10MB/s" strips only "/s" so the size parser sees "10MB" (aws-cli's
    # _human_readable_rate_to_int).
    try:
        return int(value[:-3])
    except ValueError:
        return _parse_size(value[:-2], key)
