"""`TransferConfig` - boto3's transfer configuration plus library settings.

The library re-exports this subclass of ``boto3.s3.transfer.TransferConfig``
as its public ``TransferConfig``. The transfer engine honors
``preferred_transfer_client`` (``'auto'`` / ``'classic'`` / ``'crt'``) with
boto3's own semantics (``transfer.py`` / ``crtsupport.py``; design in
``design/crt.md``); the constructor forwards it to the base class when that boto3
accepts it and otherwise keeps it as a plain attribute (the floor boto3 lacks
the field - see the signature probe below). The subclass adds the CRT tuning
knobs aws-cli keeps in its `[s3]` runtime config plus annotation staging:

- ``target_bandwidth`` - target throughput for the CRT engine in bytes per
  second (aws-cli ``target_bandwidth``; becomes the CRT client's
  ``target_throughput``). The classic engine's cap stays ``max_bandwidth``.
- ``should_stream`` / ``disk_throughput`` / ``direct_io`` - aws-cli's CRT
  file-I/O options. The pip ``s3transfer`` CRT client cannot apply them yet
  (no ``fio_options`` parameter as of 0.19); they are accepted now and passed
  through automatically once ``create_s3_crt_client`` grows support.
- `annotation_temp_dir` - directory for
  `AnnotationCopyMode.PRELOAD_TEMPFILE`; `None` delegates to the operating
  system's standard temporary-directory selection.

The extras are plain attributes (not part of the base `DEFAULTS` sentinel
machinery) and default to `None`. The CRT tuning fields are ignored by the
classic engine; `annotation_temp_dir` is consulted by the classic
multipart-copy path whenever `copy_props=ALL` staging is set up, but used
(as the tempfile location) only under `PRELOAD_TEMPFILE`. A plain
`boto3.s3.transfer.TransferConfig` remains accepted everywhere a config is
taken - readers fall back to `None` for the extra fields (and keeps boto3's own
defaults, including the one below).

One base default departs from boto3: `max_io_queue` defaults to the value
`aws s3` runs at rather than the one boto3 dials it down to. Names, order,
semantics and the `UNSET_DEFAULT` sentinel that tells an explicit value from a
defaulted one are boto3's throughout.

The profile's ``[s3]`` section of the AWS config file is aws-cli's runtime
config, ported here under aws-cli's own names (its ``transferconfig.py``):
`RuntimeConfig` parses and validates the keys, `resolve_transfer_client`
picks the engine the way aws-cli's transfer manager factory does, and
`resolve_transfer_config` turns the caller's config plus the section into the
config a run uses. Each setting comes from the first of: a value the caller
passed to `TransferConfig`, the ``[s3]`` section, aws-cli's default. A plain
``boto3.s3.transfer.TransferConfig`` counts as the caller's in every value,
since not every boto3 can tell its defaults apart, and is used as it is.
"""

from __future__ import annotations

import copy as _copy
import inspect
import logging
import os
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from boto3.s3.transfer import TransferConfig as Boto3TransferConfig

from boto3_s3 import crtsupport
from boto3_s3.awsconfig import SIZE_SUFFIX, split_size_suffix
from boto3_s3.exceptions import ConfigurationError, InvalidConfigError

if TYPE_CHECKING:
    from boto3_s3.awsconfig import AwsConfig

__all__ = ["TransferConfig"]

logger = logging.getLogger(__name__)

# The library's download IO queue depth (s3transfer's ``max_io_queue_size``:
# the cap on read parts buffered for the disk writer). s3transfer defaults it
# to 1000 and aws-cli runs there - no ``[s3]`` key maps to it - while boto3
# alone dials the same default down to 100, which would leave a slow disk
# holding a tenth of aws's readahead. The buffered ceiling is this count x
# ``io_chunksize`` (~256 MiB at the default 256 KiB) across the manager's
# downloads - one shared io executor serves them all - and is reached only when
# the disk lags the network; pass ``max_io_queue=100`` for boto3's ceiling.
_MAX_IO_QUEUE = 1000

# Back-compat (floor boto3 1.28, docs/compatibility.md): boto3's CRT
# support added the ``preferred_transfer_client`` constructor parameter only in
# boto3 ~1.33; the declared floor's base ctor does not accept it. Forwarding it
# unconditionally raises TypeError on the floor (breaking every cp/mv/sync,
# even classic with no awscrt), so probe the base signature once and, when
# absent, keep the value as a plain attribute instead - the engine reads it via
# getattr (transfer.py). On the floor the s3transfer (0.6.2) has no CRT surface,
# so 'auto' falls to classic while an explicit 'crt' is rejected by crtsupport
# (not silently downgraded). Drop this shim once the boto3 floor is raised
# past 1.33.
_BASE_ACCEPTS_PREFERRED_TRANSFER_CLIENT = (
    "preferred_transfer_client" in inspect.signature(Boto3TransferConfig.__init__).parameters
)

# Back-compat (floor boto3 1.28): the ``DEFAULTS`` table the class below
# overrides arrived with the ``UNSET_DEFAULT`` sentinel machinery (~1.42, the
# boto3 that lets ``[s3]`` tuning reach the CRT manager - compatibility.md) -
# well after the ctor grew ``preferred_transfer_client`` (the shim above).
# Every DEFAULTS-less boto3 carries concrete ctor defaults instead, so there
# the value has to be forwarded explicitly. That is safe across the whole
# band, the CRT-capable releases without the table (~1.33 - ~1.41; verified
# on 1.34.162) included, because the explicit-'crt' validation that would
# read a forwarded value as an explicitly-set classic-only option is itself
# gated on the sentinel (`create_crt_transfer_manager` submits a config to it
# only when the config carries ``UNSET_DEFAULT``). On a modern boto3 that
# validation runs, which is why the table is the only way there. Drop this
# shim only once the floor passes the table's arrival (~1.42): dropping it at
# the shim above's boundary would put the base 100 back on every DEFAULTS-less
# boto3.
_BASE_HAS_DEFAULTS = hasattr(Boto3TransferConfig, "DEFAULTS")


class TransferConfig(Boto3TransferConfig):
    """boto3's `TransferConfig` with library-only settings appended.

    The base parameters keep boto3's exact names, order, and semantics, with
    one deliberate departure: `max_io_queue` defaults to `aws s3`'s depth
    instead of boto3's. The extra settings are keyword-only and appended last,
    so existing boto3 code works unchanged.
    """

    # boto3 resolves a defaulted base parameter through this table, leaving the
    # ``UNSET_DEFAULT`` sentinel on the instance - so overriding an entry moves
    # the default without making the value look caller-supplied, and a
    # default-constructed config still passes the explicit-'crt' validation
    # that rejects classic-only options. Both spellings are listed because
    # boto3 keeps its alias and the s3transfer name in step.
    DEFAULTS: dict[str, Any] = {  # noqa: RUF012 - ClassVar: boto3 declares it plainly
        **getattr(Boto3TransferConfig, "DEFAULTS", {}),
        "max_io_queue": _MAX_IO_QUEUE,
        "max_io_queue_size": _MAX_IO_QUEUE,
    }

    def __init__(
        self,
        multipart_threshold: int | None = None,
        max_concurrency: int | None = None,
        multipart_chunksize: int | None = None,
        num_download_attempts: int | None = None,
        max_io_queue: int | None = None,
        io_chunksize: int | None = None,
        use_threads: bool | None = None,
        max_bandwidth: int | None = None,
        preferred_transfer_client: str | None = None,
        *,
        target_bandwidth: int | None = None,
        should_stream: bool | None = None,
        disk_throughput: int | None = None,
        direct_io: bool | None = None,
        annotation_temp_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        """Forward classic values and retain library settings across SDK versions."""
        # Forward each base parameter only when set, letting the base ctor supply
        # its own default. On a DEFAULTS-less boto3 (1.28 - ~1.41) the base
        # signature carries concrete defaults; forwarding None would overwrite them and
        # reach s3transfer as None - a TypeError on the first size comparison, and
        # use_threads=None silently disables threading. On a modern boto3 an
        # omitted arg resolves to the same UNSET_DEFAULT sentinel a forwarded None
        # would, so behavior is identical there.
        base_kwargs: dict[str, object] = {
            name: value
            for name, value in (
                ("multipart_threshold", multipart_threshold),
                ("max_concurrency", max_concurrency),
                ("multipart_chunksize", multipart_chunksize),
                ("num_download_attempts", num_download_attempts),
                ("max_io_queue", max_io_queue),
                ("io_chunksize", io_chunksize),
                ("use_threads", use_threads),
                ("max_bandwidth", max_bandwidth),
            )
            if value is not None
        }
        if not _BASE_HAS_DEFAULTS and max_io_queue is None:
            # Floor boto3: no DEFAULTS table to override, so the base ctor's own
            # 100 would win. Forward the library default as if it were given.
            base_kwargs["max_io_queue"] = _MAX_IO_QUEUE
        if _BASE_ACCEPTS_PREFERRED_TRANSFER_CLIENT and preferred_transfer_client is not None:
            # Same only-when-set rule as the loop above: an omitted value lets
            # the base ctor supply its own default ("auto" / its UNSET
            # sentinel), so reading config.preferred_transfer_client sees the
            # base's semantics, not a None the base never chose.
            base_kwargs["preferred_transfer_client"] = preferred_transfer_client
        # Which settings the caller gave, so `resolve_transfer_config` fills
        # only the others from ``[s3]``.
        given = {
            "multipart_threshold": multipart_threshold,
            "max_concurrency": max_concurrency,
            "multipart_chunksize": multipart_chunksize,
            "num_download_attempts": num_download_attempts,
            "max_io_queue": max_io_queue,
            "io_chunksize": io_chunksize,
            "use_threads": use_threads,
            "max_bandwidth": max_bandwidth,
            "preferred_transfer_client": preferred_transfer_client,
            "target_bandwidth": target_bandwidth,
            "should_stream": should_stream,
            "disk_throughput": disk_throughput,
            "direct_io": direct_io,
            "annotation_temp_dir": annotation_temp_dir,
        }
        super().__init__(**base_kwargs)  # pyright: ignore[reportArgumentType]
        self._given = {name: value for name, value in given.items() if value is not None}
        self._resolved = False
        if not _BASE_ACCEPTS_PREFERRED_TRANSFER_CLIENT:
            # Floor boto3 has no such field; keep it readable via getattr.
            self.preferred_transfer_client = (
                "auto" if preferred_transfer_client is None else preferred_transfer_client
            )
        self.target_bandwidth = target_bandwidth
        self.should_stream = should_stream
        self.disk_throughput = disk_throughput
        self.direct_io = direct_io
        self.annotation_temp_dir = annotation_temp_dir


# aws-cli's ``[s3]`` defaults (its ``transferconfig.py`` DEFAULTS), verbatim.
DEFAULTS: dict[str, Any] = {
    "multipart_threshold": 8 * (1024**2),
    "multipart_chunksize": 8 * (1024**2),
    "max_concurrent_requests": 10,
    "max_queue_size": 1000,
    "max_bandwidth": None,
    "preferred_transfer_client": "auto",
    "target_bandwidth": None,
    "io_chunksize": 256 * 1024,
    "should_stream": None,
    "disk_throughput": None,
    "direct_io": None,
}

# ``[s3]`` key -> `TransferConfig` constructor parameter. ``max_queue_size``
# has none (s3transfer's ``max_request_queue_size``, applied as an attribute).
_CTOR_KEYS = {
    "multipart_threshold": "multipart_threshold",
    "multipart_chunksize": "multipart_chunksize",
    "max_concurrent_requests": "max_concurrency",
    "max_bandwidth": "max_bandwidth",
    "io_chunksize": "io_chunksize",
    "target_bandwidth": "target_bandwidth",
    "should_stream": "should_stream",
    "disk_throughput": "disk_throughput",
    "direct_io": "direct_io",
}

# The ``[s3]`` keys aws-cli's CRT client consumes (its factory's
# ``_create_crt_client``); every other key is classic-only and aws-cli ignores
# it under CRT. Passing one on would also trip boto3's explicit-'crt'
# validation, which rejects ``io_chunksize`` / ``max_bandwidth`` there.
_CRT_CONSUMED_KEYS = frozenset(
    {"multipart_chunksize", "target_bandwidth", "should_stream", "disk_throughput", "direct_io"}
)

# aws-cli's transfer manager factory pins this on every classic manager.
_MAX_IN_MEMORY_CHUNKS = 6

# S3's minimum part size, which is also the threshold aws-c-s3 falls back to
# when a request carries none: ``max(part size, 5 MiB)``. aws-cli sends none.
_CRT_MIN_UPLOAD_PART_SIZE = 5 * 1024 * 1024


def human_readable_to_int(value: str) -> int:
    """Convert a human readable size (``"10MB"``) to bytes (aws-cli's function).

    Without a recognized suffix the value must be an integer string. The
    suffix table and split rule are `boto3_s3.awsconfig`'s.
    """
    value, suffix = split_size_suffix(value)
    has_size_identifier = len(value) >= 2 and suffix in SIZE_SUFFIX
    try:
        if not has_size_identifier:
            return int(value)
        return int(value[: -len(suffix)]) * SIZE_SUFFIX[suffix]
    except ValueError:
        raise InvalidConfigError(f"Invalid size value: {value}") from None


class RuntimeConfig:
    """aws-cli's ``RuntimeConfig``: merge, convert and validate the ``[s3]`` keys."""

    POSITIVE_INTEGERS: ClassVar[list[str]] = [
        "multipart_chunksize",
        "multipart_threshold",
        "max_concurrent_requests",
        "max_queue_size",
        "max_bandwidth",
        "target_bandwidth",
        "io_chunksize",
        "disk_throughput",
    ]
    HUMAN_READABLE_SIZES: ClassVar[list[str]] = [
        "multipart_chunksize",
        "multipart_threshold",
        "io_chunksize",
    ]
    HUMAN_READABLE_RATES: ClassVar[list[str]] = [
        "max_bandwidth",
        "target_bandwidth",
        "disk_throughput",
    ]
    SUPPORTED_CHOICES: ClassVar[dict[str, list[str]]] = {
        "preferred_transfer_client": ["auto", "classic", "crt"],
    }
    CHOICE_ALIASES: ClassVar[dict[str, dict[str, str]]] = {
        "preferred_transfer_client": {"default": "classic"},
    }
    BOOLEANS: ClassVar[list[str]] = ["should_stream", "direct_io"]

    @staticmethod
    def defaults() -> dict[str, Any]:
        return DEFAULTS.copy()

    def build_config(self, **kwargs: Any) -> dict[str, Any]:
        """Merge ``[s3]`` overrides over the defaults and normalize them."""
        runtime_config = DEFAULTS.copy()
        if kwargs:
            runtime_config.update(kwargs)
        self._convert_human_readable_sizes(runtime_config)
        self._convert_human_readable_rates(runtime_config)
        self._convert_booleans(runtime_config)
        self._resolve_choice_aliases(runtime_config)
        self._validate_config(runtime_config)
        return runtime_config

    def _convert_human_readable_sizes(self, runtime_config: dict[str, Any]) -> None:
        for attr in self.HUMAN_READABLE_SIZES:
            value = runtime_config.get(attr)
            if value is not None and not isinstance(value, int):
                runtime_config[attr] = human_readable_to_int(value)

    def _convert_human_readable_rates(self, runtime_config: dict[str, Any]) -> None:
        """Normalize byte and bit rates, keeping aws-cli's accepted spellings."""
        for attr in self.HUMAN_READABLE_RATES:
            value = runtime_config.get(attr)
            if value is not None and not isinstance(value, int):
                if value.endswith("B/s"):
                    runtime_config[attr] = self._human_readable_rate_to_int(value)
                elif value.endswith("b/s"):
                    bits_per_sec = self._human_readable_rate_to_int(value)
                    runtime_config[attr] = int(bits_per_sec / 8)
                elif self._is_integer_str(value):
                    runtime_config[attr] = int(value)
                else:
                    raise InvalidConfigError(
                        f"Invalid rate: {value}. The value must be expressed "
                        "as an integer in terms of bytes per second "
                        "(e.g. 10485760) or a rate in terms of bytes "
                        "per second (e.g. 10MB/s or 800KB/s) or bits per "
                        "second (e.g. 10Mb/s or 800Kb/s)"
                    )

    def _convert_booleans(self, runtime_config: dict[str, Any]) -> None:
        from botocore.utils import ensure_boolean

        for attr in self.BOOLEANS:
            value = runtime_config.get(attr)
            if value is not None:
                runtime_config[attr] = ensure_boolean(value)

    def _human_readable_rate_to_int(self, value: str) -> int:
        # "1024B/s" has no magnitude prefix, so strip "B/s"; "10MB/s" strips
        # only "/s" so the size parser sees "10MB".
        if self._is_integer_str(value[:-3]):
            return human_readable_to_int(value[:-3])
        return human_readable_to_int(value[:-2])

    def _is_integer_str(self, value: str) -> bool:
        try:
            int(value)
            return True
        except ValueError:
            return False

    def _resolve_choice_aliases(self, runtime_config: dict[str, Any]) -> None:
        """Replace accepted config aliases with their canonical choice values."""
        for attr in self.CHOICE_ALIASES:
            current_value = runtime_config.get(attr)
            if current_value in self.CHOICE_ALIASES[attr]:
                resolved_value = self.CHOICE_ALIASES[attr][current_value]
                logger.debug(
                    'Resolved %s configuration alias value "%s" to "%s"',
                    attr,
                    current_value,
                    resolved_value,
                )
                runtime_config[attr] = resolved_value

    def _validate_config(self, runtime_config: dict[str, Any]) -> None:
        self._validate_positive_integers(runtime_config)
        self._validate_choices(runtime_config)

    def _validate_positive_integers(self, runtime_config: dict[str, Any]) -> None:
        for attr in self.POSITIVE_INTEGERS:
            value = runtime_config.get(attr)
            if value is not None:
                try:
                    runtime_config[attr] = int(value)
                    if not runtime_config[attr] > 0:
                        self._error_positive_value(attr, value)
                except ValueError:
                    self._error_positive_value(attr, value)

    def _validate_choices(self, runtime_config: dict[str, Any]) -> None:
        for attr in self.SUPPORTED_CHOICES:
            value = runtime_config.get(attr)
            if value is not None and value not in self.SUPPORTED_CHOICES[attr]:
                self._error_invalid_choice(attr, value)

    def _error_positive_value(self, name: str, value: Any) -> None:
        raise InvalidConfigError(f"Value for {name} must be a positive integer: {value}")

    def _error_invalid_choice(self, name: str, value: Any) -> None:
        raise InvalidConfigError(
            f'Invalid value: "{value}" for configuration option: "{name}". '
            f"Supported values are: {', '.join(self.SUPPORTED_CHOICES[name])}"
        )


def load_scoped_s3_config(config: AwsConfig) -> dict[str, Any]:
    """Read aws-cli's ``[s3]`` keys from the active profile of *config*.

    Unknown keys are ignored, as aws-cli reads none of them.
    """
    scoped: dict[str, Any] = {}
    for key in DEFAULTS:
        value = config.get_str(f"s3.{key}")
        if value is not None:
            scoped[key] = value
    return scoped


def _crt_unavailable_message() -> str:
    """The refusal for an explicit ``crt`` whose awscrt is absent or too old.

    The remedies differ: an absent awscrt is the missing extra, while one below
    boto3's CRT minimum is what the extra itself brings on an old boto3, so
    only a newer boto3 helps there.
    """
    try:
        import awscrt
    except ImportError:
        return (
            "preferred_transfer_client is set to crt but awscrt is "
            "unavailable. Install the optional CRT dependency with the "
            "'crt' extra (e.g. pip install 'boto3-s3[crt]')."
        )
    return (
        "preferred_transfer_client is set to crt but the installed awscrt "
        f"{getattr(awscrt, '__version__', '')} is too old for it (the CRT transfer "
        "engine needs boto3 >= 1.34.6 with its crt extra). Upgrade boto3 "
        "(e.g. pip install -U 'boto3[crt]>=1.34.6')."
    )


def resolve_transfer_client(preferred: str, *, copy: bool) -> Literal["classic", "crt"]:
    """Pick the transfer engine the way aws-cli's transfer manager factory does.

    An S3-to-S3 copy is always classic (the CRT engine has no copy). An
    explicit ``crt`` needs a usable awscrt and s3transfer - otherwise a
    `ConfigurationError` - and claims the process's CRT slot best-effort, as
    aws-cli does. ``auto`` is CRT only on a host the CRT is optimized for and
    when this process gets the slot. Any other preference is classic.
    """
    if copy:
        return "classic"
    preferred = preferred.lower()
    if preferred == "crt":
        if not crtsupport.has_minimum_crt_version():
            raise ConfigurationError(_crt_unavailable_message())
        if not crtsupport.has_crt_s3transfer():
            raise ConfigurationError(
                "preferred_transfer_client is set to crt but the installed "
                "s3transfer is too old for it (needs s3transfer >= 0.8.0). "
                "Upgrade s3transfer (e.g. via a newer boto3)."
            )
        crtsupport.acquire_process_lock()
        return "crt"
    if (
        preferred == "auto"
        and crtsupport.has_crt_s3transfer()
        and crtsupport.is_optimized_for_system()
        and crtsupport.acquire_process_lock()
    ):
        return "crt"
    return "classic"


def resolve_transfer_config(
    aws_config: AwsConfig, config: Boto3TransferConfig | None, *, copy: bool
) -> Boto3TransferConfig:
    """The config a ``cp`` / ``mv`` / ``sync`` / ``rm`` run uses.

    Each setting comes from *config* when the caller set it - in the
    constructor or as an attribute afterwards - else from the ``[s3]`` section
    *aws_config* reads, else from aws-cli's default; the engine is then picked
    by `resolve_transfer_client`. As aws-cli builds its managers, ``[s3]`` keys
    the chosen engine does not consume are left out, an ``[s3]`` part size
    under CRT also sets the threshold aws-c-s3 would fall back to, and a
    classic engine keeps aws-cli's in-memory chunk caps.

    The result is a copy of *config* (a fresh `TransferConfig` without one), so
    a subclass and the caller's own attributes carry over. A plain boto3
    `TransferConfig` is returned as it is: every value in it counts as the
    caller's. So is a config this function already returned.
    """
    if config is not None and not isinstance(config, TransferConfig):
        return config
    if config is not None and config._resolved:  # pyright: ignore[reportPrivateUsage]
        return config
    effective = _copy.copy(config) if config is not None else TransferConfig()
    given = _given_settings(effective)
    scoped = load_scoped_s3_config(aws_config)
    runtime_config = RuntimeConfig().build_config(**scoped)
    if "preferred_transfer_client" in given:
        preferred = str(effective.preferred_transfer_client)
    else:
        preferred = runtime_config["preferred_transfer_client"]
    resolved = resolve_transfer_client(preferred, copy=copy)
    crt = resolved == "crt"
    effective.preferred_transfer_client = resolved
    # boto3 rejects classic-only settings under an explicit 'crt' only; a crt
    # the caller did not ask for is aws-cli's or auto's choice, under which the
    # CRT engine just ignores them, as aws-cli's does.
    setattr(effective, "_crt_requested", crt and "preferred_transfer_client" in given)  # noqa: B010
    for rc_key, ctor_key in _CTOR_KEYS.items():
        if ctor_key in given or rc_key not in scoped:
            continue
        if crt and rc_key not in _CRT_CONSUMED_KEYS:
            continue
        setattr(effective, ctor_key, runtime_config[rc_key])
    if (
        crt
        and "multipart_chunksize" in scoped
        and "multipart_chunksize" not in given
        and "multipart_threshold" not in given
    ):
        # The installed s3transfer stamps the config's threshold on every CRT
        # put, where aws-cli sends none and aws-c-s3 falls back to
        # max(part size, 5 MiB); pin that fallback so the same files go
        # multipart.
        effective.multipart_threshold = max(
            runtime_config["multipart_chunksize"], _CRT_MIN_UPLOAD_PART_SIZE
        )
    if not crt:
        defaults = _attribute_defaults()
        if (
            "max_queue_size" in scoped
            and effective.max_request_queue_size == defaults["max_request_queue_size"]
        ):
            effective.max_request_queue_size = runtime_config["max_queue_size"]
        for attr in ("max_in_memory_upload_chunks", "max_in_memory_download_chunks"):
            if getattr(effective, attr) == defaults[attr]:
                setattr(effective, attr, _MAX_IN_MEMORY_CHUNKS)
    effective._resolved = True  # pyright: ignore[reportPrivateUsage]
    return effective


# The settings `TransferConfig`'s constructor takes, with boto3's alias for the
# attribute s3transfer reads where it has one.
_SETTINGS = {
    "multipart_threshold": None,
    "max_concurrency": "max_request_concurrency",
    "multipart_chunksize": None,
    "num_download_attempts": None,
    "max_io_queue": "max_io_queue_size",
    "io_chunksize": None,
    "use_threads": None,
    "max_bandwidth": None,
    "preferred_transfer_client": None,
    "target_bandwidth": None,
    "should_stream": None,
    "disk_throughput": None,
    "direct_io": None,
    "annotation_temp_dir": None,
}


def _raw(config: Any, attr: str) -> Any:
    """*attr* as stored, boto3's ``UNSET_DEFAULT`` sentinel included."""
    get_deep_attr = getattr(config, "get_deep_attr", None)
    if get_deep_attr is not None:
        try:
            return get_deep_attr(attr)
        except AttributeError:
            return None
    return getattr(config, attr, None)


def _given_settings(config: TransferConfig) -> set[str]:
    """The settings the caller gave *config*, in its constructor or afterwards.

    A constructor argument is recorded (`TransferConfig._given`); an attribute
    set afterwards is found by comparing the stored value - under either of
    boto3's names for it - with a fresh config's, whose defaulted values are
    boto3's ``UNSET_DEFAULT`` sentinel where boto3 has one.
    """
    given = set(config._given)  # pyright: ignore[reportPrivateUsage]
    fresh = TransferConfig()
    for name, alias in _SETTINGS.items():
        for attr in (name, alias):
            if attr is None:
                continue
            value, default = _raw(config, attr), _raw(fresh, attr)
            if value is not default and value != default:
                given.add(name)
    return given


# s3transfer's settings that boto3's constructor does not take.
_ATTRIBUTE_ONLY = (
    "max_request_queue_size",
    "max_submission_queue_size",
    "max_submission_concurrency",
    "max_in_memory_upload_chunks",
    "max_in_memory_download_chunks",
)


def _attribute_defaults() -> dict[str, Any]:
    """s3transfer's defaults for `_ATTRIBUTE_ONLY`."""
    fresh = Boto3TransferConfig()
    return {attr: getattr(fresh, attr) for attr in _ATTRIBUTE_ONLY}
