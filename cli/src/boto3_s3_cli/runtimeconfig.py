"""The ``[s3]`` runtime config - aws-cli's ``transferconfig.py`` ported.

``aws s3`` reads transfer tuning from the profile's ``s3`` section
(``~/.aws/config``): the classic engine knobs (``multipart_threshold``,
``max_concurrent_requests`` ...), the CRT engine knobs (``target_bandwidth``,
the file-I/O options) and the engine switch itself
(``preferred_transfer_client`` - a config key only, aws-cli has no CLI
option for it). ``RuntimeConfig`` is the aws-cli parser verbatim -
human-readable sizes (``8MB``), rates (``100MB/s`` / ``800Kb/s``), booleans,
the ``default`` -> ``classic`` alias, and byte-exact error wording - raising
the library's ``InvalidConfigError`` (aws-cli's class of the same name)
where aws-cli's escapes to its general handler (both exit 255, after every
path/usage validation).

``load_scoped_s3_config`` reads the section the way aws-cli's
``_get_runtime_config`` does - the profile's scoped config, so nested
``s3 =`` INI syntax, ``AWS_CONFIG_FILE`` and ``--profile`` all behave like
aws. The engine decision tree over the parsed config is
``resolve_transfer_client`` below (a port of aws-cli
``TransferManagerFactory._compute_transfer_client_type``; docs/crt.md
section 4), driven from ``commands/transferargs.resolve_transfer_config``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from boto3_s3 import ConfigurationError, InvalidConfigError
from boto3_s3.awsconfig import SIZE_SUFFIX, AwsConfig, split_size_suffix

if TYPE_CHECKING:
    from boto3_s3 import TransferConfig

logger = logging.getLogger(__name__)

# RuntimeConfig key -> boto3 TransferConfig constructor parameter. ``[s3]``
# keys boto3's constructor does not expose (``max_queue_size``) are applied as
# attributes after construction (``build_transfer_config``).
_TRANSFER_CONFIG_CTOR_KEYS = {
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

# aws-cli's TransferManagerFactory pins this on every classic manager.
_MAX_IN_MEMORY_CHUNKS = 6

# The ``[s3]`` keys the CRT engine actually consumes (aws-cli factory
# ``_create_crt_client``: the part size, the throughput target and the file-I/O
# options). Every other key is classic-only - aws-cli silently ignores it under
# CRT. Forwarding the classic-only knobs onto a crt-preferred ``TransferConfig``
# is not merely wasteful: boto3's ``_validate_crt_transfer_config`` rejects the
# ones that live in its ``DEFAULTS`` but outside its CRT allow-list
# (``io_chunksize`` / ``max_bandwidth``), which would surface as an uncaught
# error where aws-cli completes the transfer (crt.md section 4).
_CRT_CONSUMED_KEYS = frozenset(
    {
        "multipart_chunksize",
        "target_bandwidth",
        "should_stream",
        "disk_throughput",
        "direct_io",
    }
)

# aws-cli DEFAULTS (awscli/customizations/s3/transferconfig.py), verbatim.
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


def human_readable_to_int(value: str) -> int:
    """Convert a human readable size (``"10MB"``) to bytes (aws-cli port).

    A verbatim port under aws-cli's own function name (overview.md section 3,
    symbol-name traceability), aws's boundary behavior included; the suffix
    table and split rule are shared with the library
    (``boto3_s3.awsconfig.SIZE_SUFFIX`` / ``split_size_suffix``), whose
    ``_parse_size`` is deliberately hardened where this stays aws-faithful.
    Without a recognized suffix the value must be an integer string; the
    failure wording is aws-cli's (it reaches the user via rc 255).
    """
    value, suffix = split_size_suffix(value)
    has_size_identifier = len(value) >= 2 and suffix in SIZE_SUFFIX
    if not has_size_identifier:
        try:
            return int(value)
        except ValueError:
            raise InvalidConfigError(f"Invalid size value: {value}") from None
    return int(value[: -len(suffix)]) * SIZE_SUFFIX[suffix]


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
        """Normalize byte and bit rates, preserving aws-cli's accepted spellings."""
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
        # Import botocore only when converting an actual runtime config.
        from botocore.utils import ensure_boolean

        for attr in self.BOOLEANS:
            value = runtime_config.get(attr)
            if value is not None:
                runtime_config[attr] = ensure_boolean(value)

    def _human_readable_rate_to_int(self, value: str) -> int:
        # "1024B/s" has no magnitude prefix -> strip "B/s"; "10MB/s" strips
        # only "/s" so the size parser sees "10MB" (aws-cli comment, condensed).
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
        """Validate normalized positive-integer and enumerated settings."""
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
            if value is not None:
                if value not in self.SUPPORTED_CHOICES[attr]:
                    self._error_invalid_choice(attr, value)

    def _error_positive_value(self, name: str, value: Any) -> None:
        raise InvalidConfigError(f"Value for {name} must be a positive integer: {value}")

    def _error_invalid_choice(self, name: str, value: Any) -> None:
        raise InvalidConfigError(
            f'Invalid value: "{value}" for configuration option: "{name}". '
            f"Supported values are: {', '.join(self.SUPPORTED_CHOICES[name])}"
        )


def load_scoped_s3_config(config: AwsConfig) -> dict[str, Any]:
    """Read aws-cli's known ``[s3]`` keys from an `S3`-bound config reader.

    The caller obtains *config* from `S3.aws_config`, so the active profile,
    config file, and parsed config cache are those of the exact session the
    command's clients use. Unknown ``[s3]`` keys are intentionally ignored,
    matching `build_transfer_config`, which consumes only aws-cli's declared
    runtime keys.
    """
    scoped: dict[str, Any] = {}
    for key in DEFAULTS:
        value = config.get_str(f"s3.{key}")
        if value is not None:
            scoped[key] = value
    return scoped


def resolve_transfer_client(
    runtime_config: dict[str, Any], *, paths_type: str
) -> Literal["classic", "crt"]:
    """Pick the transfer engine the way aws-cli's factory does.

    Ports ``TransferManagerFactory._compute_transfer_client_type``: an s3->s3
    copy is unconditionally classic (the CRT manager has no copy); otherwise
    the ``[s3] preferred_transfer_client`` decides, with ``auto`` resolved
    against the host (``is_optimized_for_system`` + the process lock). The one
    aws-cli cannot express - awscrt absent under an explicit ``crt`` (aws
    bundles it) - is the CLI's documented degradation: a configuration error,
    not a traceback (boto3's ``MissingDependencyException`` would otherwise
    escape ``main``).
    """
    if paths_type == "s3s3":
        return "classic"
    preferred = runtime_config["preferred_transfer_client"]  # alias already resolved
    # Deferred awscrt-touching import: only the transfer path reaches here.
    from boto3_s3 import crtsupport

    if preferred == "crt":
        if not crtsupport.has_minimum_crt_version():
            raise ConfigurationError(
                "preferred_transfer_client is set to crt but awscrt is "
                "unavailable. Install the optional CRT dependency with the "
                "'crt' extra (e.g. pip install 'boto3-s3-cli[crt]')."
            )
        if not crtsupport.has_crt_s3transfer():
            # The floor s3transfer (< 0.8.0) lacks the CRT lock/credentials
            # surface; fail clean (like awscrt-missing) instead of an ImportError.
            raise ConfigurationError(
                "preferred_transfer_client is set to crt but the installed "
                "s3transfer is too old for it (needs s3transfer >= 0.8.0). "
                "Upgrade s3transfer (e.g. via a newer boto3)."
            )
        crtsupport.acquire_process_lock()  # aws-cli claims the slot, proceeds regardless
        return "crt"
    if (
        preferred == "auto"
        and crtsupport.has_crt_s3transfer()
        and crtsupport.is_optimized_for_system()
    ):
        if crtsupport.acquire_process_lock():
            return "crt"
    return "classic"


def build_transfer_config(
    scoped: dict[str, Any],
    runtime_config: dict[str, Any],
    resolved: Literal["classic", "crt"],
) -> TransferConfig:
    """Build the library ``TransferConfig`` from the parsed ``[s3]`` config.

    Only keys the user actually set in ``[s3]`` (``scoped``) are passed to the
    constructor, carrying the converted values from ``runtime_config``; unset
    keys keep boto3's ``UNSET_DEFAULT`` sentinel, which is what lets the CRT
    engine honor aws-cli's "use ``multipart_chunksize`` as the part size only
    when it was set explicitly" rule. ``preferred_transfer_client`` carries
    the already-resolved engine (so the library does not re-resolve ``auto``).

    The config is engine-specific, exactly like aws-cli (aws-cli factory builds
    the classic ``TransferConfig`` and the CRT client from separate key sets).
    Under CRT only the keys the CRT client consumes (``_CRT_CONSUMED_KEYS``)
    are forwarded, and the classic-only tuning (the request queue size and the
    in-memory chunk caps) is omitted - both because the CRT manager ignores it
    and because forwarding ``io_chunksize`` / ``max_bandwidth`` would trip
    boto3's CRT config validation (crt.md section 4). Under classic every key flows
    through and the aws-cli in-memory chunk caps are pinned.
    """
    from boto3_s3 import TransferConfig

    crt = resolved == "crt"
    kwargs: dict[str, Any] = {"preferred_transfer_client": resolved}
    for rc_key, ctor_key in _TRANSFER_CONFIG_CTOR_KEYS.items():
        if rc_key not in scoped:
            continue
        if crt and rc_key not in _CRT_CONSUMED_KEYS:
            # aws-cli's CRT client never reads this classic-only knob; placing
            # io_chunksize / max_bandwidth here would additionally trip boto3's
            # _validate_crt_transfer_config (a traceback where aws exits 0).
            continue
        kwargs[ctor_key] = runtime_config[rc_key]
    config = TransferConfig(**kwargs)
    if not crt:
        # Classic-only tuning aws-cli applies solely to its classic
        # TransferManager (aws-cli factory): the request queue size boto3's
        # constructor does not expose (s3transfer's max_request_queue_size)
        # and the in-memory chunk caps. The CRT manager ignores both.
        if "max_queue_size" in scoped:
            config.max_request_queue_size = runtime_config["max_queue_size"]
        config.max_in_memory_upload_chunks = _MAX_IN_MEMORY_CHUNKS
        config.max_in_memory_download_chunks = _MAX_IN_MEMORY_CHUNKS
    return config
