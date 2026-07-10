"""CRT transfer-engine resolution - boto3's ``boto3/crt.py`` for this library.

``Transferrer`` resolves ``TransferConfig.preferred_transfer_client``
(``'auto'`` / ``'classic'`` / ``'crt'``) with boto3's own semantics, and this
module is the machinery behind the non-classic answers: a process-wide
singleton CRT S3 client plus request serializer, guarded by a cross-process
lock so one host never runs several CRT clients for this application
(lock name ``'boto3-s3'`` - boto3 uses ``'boto3'``, aws-cli ``'aws-cli'``;
each product namespaces its own lock).

The shape follows ``boto3/crt.py`` closely (singleton, lock-or-classic,
same-identity compatibility check, ``MissingDependencyException`` for an
explicit ``'crt'`` without a usable awscrt). Documented deviations, all
required by this library's connection model where the caller owns the
client (docs/crt.md):

- **endpoint**: boto3 always passes ``endpoint_url=None`` to the serializer,
  which breaks custom endpoints (MinIO et al). We derive the endpoint from
  ``client.meta.endpoint_url`` - kept for custom hosts, ``None`` for the AWS
  default form (recognized across all botocore partitions, not just the two
  commercial suffixes) so botocore re-resolves per request exactly like boto3.
  ``use_ssl`` follows the custom endpoint's scheme (aws-cli only honors an
  ``--endpoint-url`` argument here; deriving from the resolved client also
  covers ``AWS_ENDPOINT_URL_S3``).
- **verify / unsigned**: the TLS verification setting and ``--no-sign-request``
  are recovered from the client (aws-cli wires them from CLI params; boto3
  ignores both). Reading them rides on private botocore attributes at the
  same level as ``client._get_credentials()``, which boto3 itself uses.
- **compatibility**: the singleton additionally pins the derived endpoint and
  the signed/unsigned mode; a later client that disagrees falls back to
  classic, same as boto3's region/credentials mismatch.

Nothing here imports awscrt or ``s3transfer.crt`` at module import time; the
classic path never pays for them (docs/imports.md).
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

logger = logging.getLogger(__name__)

__all__ = [
    "PROCESS_LOCK_NAME",
    "acquire_process_lock",
    "create_crt_transfer_manager",
    "has_crt_s3transfer",
    "has_minimum_crt_version",
    "is_optimized_for_system",
    "should_use_crt",
]

PROCESS_LOCK_NAME = "boto3-s3"

_MINIMUM_CRT_VERSION = (0, 19, 18)

# boto3/crt.py's allow-list for explicit-'crt' config validation: every other
# TransferConfig option is classic-only and must not be explicitly set.
_ALLOWED_CRT_TRANSFER_CONFIG_OPTIONS = {
    "multipart_threshold",
    "max_concurrency",
    "max_request_concurrency",
    "multipart_chunksize",
    "preferred_transfer_client",
}

# Process-wide singletons (boto3/crt.py shape). The first client to reach the
# CRT path wins the process; ``None`` means "not initialized yet or the lock
# was unavailable" and is re-attempted on the next call, like boto3.
_CREATION_LOCK = threading.Lock()
_crt_s3_client: _CrtS3Client | None = None
_crt_serializer: Any | None = None


class _CrtS3Client:
    """The held CRT client plus the identity it was created for.

    Mirrors boto3's ``CRTS3Client``: the CRT client can only sign for one
    region/credentials (and, in our wiring, one endpoint and signing mode),
    so the tuple is pinned to reject incompatible later clients.
    """

    def __init__(
        self,
        crt_client: Any,
        process_lock: Any,
        region: str,
        endpoint_url: str | None,
        cred_wrapper: Any | None,
    ) -> None:
        self.crt_client = crt_client
        self.process_lock = process_lock
        self.region = region
        self.endpoint_url = endpoint_url
        self.cred_wrapper = cred_wrapper  # None = unsigned client


def has_minimum_crt_version() -> bool:
    """Whether awscrt is importable at the version boto3's CRT path requires."""
    try:
        import awscrt
    except ImportError:
        return False
    try:
        version = tuple(int(part) for part in awscrt.__version__.split("."))
    except (TypeError, ValueError):
        return False
    return version >= _MINIMUM_CRT_VERSION


def has_crt_s3transfer() -> bool:
    """Whether the installed ``s3transfer.crt`` exposes the surface the CRT path
    needs. The process lock, the botocore credentials wrapper, and the
    ``create_s3_crt_client(crt_credentials_provider=...)`` parameter all landed in
    s3transfer 0.8.0; below it (the floor is 0.6.2) they are absent, so the CRT
    manager cannot be built and the engine must degrade to classic. Probed here
    so the CRT decision (library and CLI) never reaches an ImportError."""
    try:
        import s3transfer.crt as crt
    except ImportError:
        return False
    return hasattr(crt, "acquire_crt_s3_process_lock") and hasattr(
        crt, "BotocoreCRTCredentialsWrapper"
    )


def is_optimized_for_system() -> bool:
    """awscrt's host-optimization probe; ``False`` without a usable awscrt.

    The aws-cli auto-resolution gate (aws-cli's ``TransferManagerFactory``): a
    host the CRT is tuned for. Separate from ``should_use_crt`` so the
    CLI can port the aws-cli decision tree primitive-by-primitive.
    """
    if not has_minimum_crt_version():
        return False
    import awscrt.s3

    return bool(awscrt.s3.is_optimized_for_system())


def acquire_process_lock() -> bool:
    """Claim this process's CRT slot; ``False`` if another process holds it.

    The lock is process-global in ``s3transfer`` (stored and reused), so the
    CLI's auto-resolution claim and the library's later
    ``create_crt_transfer_manager`` acquisition return the same lock.
    """
    from s3transfer.crt import acquire_crt_s3_process_lock

    # Typed as the lock object, but the documented contract returns None when
    # another process holds it (same Any-cast as _initialize).
    lock: Any = acquire_crt_s3_process_lock(PROCESS_LOCK_NAME)
    return lock is not None


def should_use_crt(preferred: str) -> bool:
    """boto3's ``_should_use_crt`` over an already-extracted preference.

    Raises botocore's ``MissingDependencyException`` (boto3's wording) when
    ``'crt'`` is explicit but awscrt is missing or too old; otherwise answers
    whether the CRT manager should be attempted (``'auto'`` requires
    ``awscrt.s3.is_optimized_for_system()``).
    """
    pref = preferred.lower()
    has_min_crt = has_minimum_crt_version()
    # The floor s3transfer (< 0.8.0) lacks the CRT lock / credentials surface, so
    # even with awscrt present the CRT manager cannot be built - treat it as not
    # ready (degrade for auto, clear error for explicit crt) instead of crashing.
    crt_ready = has_min_crt and has_crt_s3transfer()
    if pref == "crt" and not crt_ready:
        from botocore.exceptions import MissingDependencyException

        if not has_min_crt:
            try:
                import awscrt

                installed = True
                msg_version = f", with version: {awscrt.__version__}"
            except ImportError:
                installed = False
                msg_version = ""
            raise MissingDependencyException(
                msg=(
                    "CRT transfer client is configured but is missing minimum CRT "
                    f"version. CRT installed: {installed}{msg_version}"
                )
            )
        raise MissingDependencyException(
            msg=(
                "CRT transfer client is configured but the installed s3transfer is "
                "too old for it (needs s3transfer >= 0.8.0; the floor is 0.6.2)."
            )
        )
    is_optimized = False
    if crt_ready:
        import awscrt.s3

        is_optimized = bool(awscrt.s3.is_optimized_for_system())
    if (is_optimized and pref == "auto") or pref == "crt":
        return True
    logger.debug(
        "Opting out of the CRT transfer manager. Preferred client: %s, "
        "CRT available: %s, instance optimized: %s",
        pref,
        has_min_crt,
        is_optimized,
    )
    return False


def create_crt_transfer_manager(
    client: S3Client, config: Any | None, *, endpoint: str | None = None
) -> Any | None:
    """Return a ``CRTTransferManager`` for ``client``, or ``None`` for classic.

    ``None`` means the boto3-faithful fallbacks fired: the cross-process lock
    is held elsewhere, or the process singleton was created for a different
    region / credentials / endpoint / signing mode.

    ``endpoint`` is the caller's explicit endpoint (the CLI threads its
    ``--endpoint-url`` here, matching aws-cli, which passes it to the CRT
    serializer verbatim - so a custom endpoint under an AWS domain, e.g. a VPC
    interface endpoint, is honored rather than re-resolved to public S3). When
    it is ``None`` the host-heuristic default (``_derive_endpoint``) decides,
    which is boto3-faithful for a real AWS endpoint and still pins a non-AWS
    custom host such as MinIO.
    """
    crt_s3_client = _get_crt_s3_client(client, config, endpoint)
    if not _is_compatible_request(client, crt_s3_client, endpoint):
        return None
    assert crt_s3_client is not None
    from s3transfer.crt import CRTTransferManager

    kwargs: dict[str, Any] = {
        "crt_s3_client": crt_s3_client.crt_client,
        "crt_request_serializer": _crt_serializer,
    }
    # Back-compat shim (floor s3transfer 0.6.2, docs/overview.md section 2):
    # CRTTransferManager grew its ``config`` kwarg only in s3transfer 0.16.0, so
    # passing it to an older one raises TypeError. boto3 gates this on
    # ``TRANSFER_CONFIG_SUPPORTS_CRT = hasattr(TransferConfig, "UNSET_DEFAULT")``;
    # that sentinel landed in the same boto3 release that raised boto3's own
    # s3transfer floor to 0.16, and boto3 pins s3transfer tightly, so a config
    # carrying ``UNSET_DEFAULT`` is a reliable proxy for "this s3transfer accepts
    # config". Drop this shim once the s3transfer floor is raised past 0.16.
    if config is not None and hasattr(config, "UNSET_DEFAULT"):
        _validate_crt_transfer_config(config)
        kwargs["config"] = config
    elif config is not None:
        # boto3-faithful (crt.md): the config cannot reach this s3transfer, so
        # warn instead of silently dropping it, matching boto3's own wording.
        logger.warning(
            "Using TransferConfig with CRT client requires "
            "s3transfer >= 0.16.0, configured values will be ignored."
        )
    return CRTTransferManager(**kwargs)


def _get_crt_s3_client(
    client: S3Client, config: Any | None, endpoint: str | None
) -> _CrtS3Client | None:
    global _crt_s3_client, _crt_serializer
    with _CREATION_LOCK:
        if _crt_s3_client is None:
            serializer, crt_s3_client = _initialize(client, config, endpoint)
            _crt_serializer = serializer
            _crt_s3_client = crt_s3_client
    return _crt_s3_client


def _initialize(
    client: S3Client, config: Any | None, endpoint: str | None
) -> tuple[Any, _CrtS3Client] | tuple[None, None]:
    from s3transfer.crt import (
        BotocoreCRTRequestSerializer,
        acquire_crt_s3_process_lock,
        create_s3_crt_client,
    )

    # Annotated Any: s3transfer types the return as the lock object, but the
    # documented contract is "None when another process holds it".
    lock: Any = acquire_crt_s3_process_lock(PROCESS_LOCK_NAME)
    if lock is None:
        # Another process of this application holds the CRT client; classic.
        return None, None

    from botocore.session import Session

    region = client.meta.region_name
    endpoint_url = _resolve_endpoint(client, endpoint)
    serializer = BotocoreCRTRequestSerializer(
        Session(), {"region_name": region, "endpoint_url": endpoint_url}
    )

    create_kwargs: dict[str, Any] = {
        "region": region,
        "use_ssl": _derive_use_ssl(endpoint_url),
        "verify": _derive_verify(client),
        # aws-cli passes None when multipart_chunksize is not explicit so the
        # CRT picks the part size dynamically; s3transfer's own default is a
        # fixed 8 MiB, hence always passing the key.
        "part_size": _explicit_chunksize(config),
        "target_throughput": getattr(config, "target_bandwidth", None),
    }
    cred_wrapper = _credentials_wrapper(client)
    if cred_wrapper is not None:
        create_kwargs["crt_credentials_provider"] = cred_wrapper.to_crt_credentials_provider()
    _add_fio_options(create_kwargs, config, create_s3_crt_client)
    crt_client = create_s3_crt_client(**create_kwargs)
    return serializer, _CrtS3Client(crt_client, lock, region, endpoint_url, cred_wrapper)


def _is_compatible_request(
    client: S3Client, crt_s3_client: _CrtS3Client | None, endpoint: str | None
) -> bool:
    """boto3's ``is_crt_compatible_request`` plus the endpoint/signing pins."""
    if crt_s3_client is None:
        return False
    if client.meta.region_name != crt_s3_client.region:
        return False
    if _resolve_endpoint(client, endpoint) != crt_s3_client.endpoint_url:
        return False
    if _is_unsigned(client):
        return crt_s3_client.cred_wrapper is None
    if crt_s3_client.cred_wrapper is None:
        return False
    boto3_creds = _client_credentials(client)
    if boto3_creds is None:
        return False
    return _compare_identity(boto3_creds.get_frozen_credentials(), crt_s3_client.cred_wrapper)


def _compare_identity(frozen_creds: Any, cred_wrapper: Any) -> bool:
    from botocore.exceptions import NoCredentialsError

    try:
        crt_creds = cred_wrapper()
    except NoCredentialsError:
        return False
    return bool(
        frozen_creds.access_key == crt_creds.access_key_id
        and frozen_creds.secret_key == crt_creds.secret_access_key
        and frozen_creds.token == crt_creds.session_token
    )


def _validate_crt_transfer_config(config: Any) -> None:
    """boto3's explicit-'crt' validation: classic-only options must be unset."""
    if config.preferred_transfer_client != "crt":
        return
    invalid_crt_args = [
        param
        for param in config.DEFAULTS
        if param not in _ALLOWED_CRT_TRANSFER_CONFIG_OPTIONS
        and config.get_deep_attr(param) is not config.UNSET_DEFAULT
    ]
    if invalid_crt_args:
        from boto3.exceptions import InvalidCrtTransferConfigError

        raise InvalidCrtTransferConfigError(
            "The following transfer config options are invalid "
            "when preferred_transfer_client is set to crt: "
            f"{', '.join(invalid_crt_args)}`"
        )


_aws_dns_suffixes_cache: frozenset[str] | None = None


def _aws_dns_suffixes() -> frozenset[str]:
    """The host suffixes botocore resolves AWS's own endpoints under.

    Collected once (memoized) from the installed botocore's endpoint data -
    every partition's ``dnsSuffix`` plus its dualstack / fips variant suffixes:
    ``aws`` (amazonaws.com, api.aws), ``aws-cn`` (amazonaws.com.cn), ``aws-us-gov``,
    ``aws-eusc`` (amazonaws.eu), and the iso partitions (c2s.ic.gov,
    sc2s.sgov.gov, cloud.adc-e.uk, csp.hci.ic.gov). A standard endpoint in any
    of those partitions is recognized, not just the two commercial suffixes.
    Any host outside this set - a custom host (MinIO, gateways), or a partition
    newer than the installed botocore - is treated as custom and pinned.
    """
    global _aws_dns_suffixes_cache
    if _aws_dns_suffixes_cache is None:
        from botocore.loaders import create_loader

        suffixes: set[str] = set()
        data = create_loader().load_data("endpoints")
        for partition in data["partitions"]:
            suffixes.add(partition["dnsSuffix"])
            for variant in partition.get("defaults", {}).get("variants", []):
                suffix = variant.get("dnsSuffix")
                if suffix:
                    suffixes.add(suffix)
        _aws_dns_suffixes_cache = frozenset(suffixes)
    return _aws_dns_suffixes_cache


def _derive_endpoint(client: S3Client) -> str | None:
    """Custom endpoints ride into the CRT wiring; the AWS default form stays None.

    ``None`` keeps boto3's behavior - botocore re-resolves the AWS endpoint
    per request - while a custom host (MinIO, gateways) must be pinned or the
    CRT client would dial the real AWS endpoint. The default form is recognized
    across every botocore partition (see `_aws_dns_suffixes`), so a standard
    endpoint outside the two commercial suffixes is not mistaken for a custom
    host and needlessly pinned.
    """
    endpoint = client.meta.endpoint_url
    host = urlsplit(endpoint).hostname or ""
    if any(host == suffix or host.endswith("." + suffix) for suffix in _aws_dns_suffixes()):
        return None
    return endpoint


def _resolve_endpoint(client: S3Client, endpoint: str | None) -> str | None:
    """The endpoint the CRT client pins for this request.

    An explicit ``endpoint`` (the caller's ``--endpoint-url``) is honored
    verbatim, matching aws-cli - so a custom endpoint that happens to sit under
    an AWS DNS suffix (a VPC interface endpoint, a FIPS / dualstack host the user
    named directly, ...) is pinned rather than dropped. ``None`` (no explicit
    endpoint) falls back to the host heuristic, which is boto3-faithful for a
    resolved AWS endpoint and still pins a non-AWS custom host such as MinIO.
    """
    return _derive_endpoint(client) if endpoint is None else endpoint


def _derive_use_ssl(endpoint_url: str | None) -> bool:
    if endpoint_url is None:
        return True
    return urlsplit(endpoint_url).scheme != "http"


def _derive_verify(client: S3Client) -> Any:
    """Map botocore's verify setting onto ``create_s3_crt_client``'s contract.

    The CRT factory takes ``None`` (default trust store), ``False`` (skip
    verification) or a CA-bundle path - never ``True``, which botocore uses
    for its own default.
    """
    verify = getattr(
        getattr(getattr(client, "_endpoint", None), "http_session", None), "_verify", None
    )
    if verify is True or verify is None:
        return None
    return verify


def _is_unsigned(client: S3Client) -> bool:
    from botocore import UNSIGNED

    config: Any = client.meta.config  # botocore Config: signature_version is untyped
    return config.signature_version is UNSIGNED


def _client_credentials(client: S3Client) -> Any:
    accessor: Any = client  # boto3/crt.py rides the same private accessor
    return accessor._get_credentials()


def _credentials_wrapper(client: S3Client) -> Any | None:
    if _is_unsigned(client):
        return None
    from s3transfer.crt import BotocoreCRTCredentialsWrapper

    return BotocoreCRTCredentialsWrapper(_client_credentials(client))


def _explicit_chunksize(config: Any | None) -> int | None:
    if config is None or not hasattr(config, "get_deep_attr"):
        return None
    value = config.get_deep_attr("multipart_chunksize")
    if value is config.UNSET_DEFAULT:
        return None
    return value


def _add_fio_options(kwargs: dict[str, Any], config: Any | None, create_fn: Any) -> None:
    """aws-cli's file-I/O options, applied only once s3transfer can take them.

    pip s3transfer (<=0.17) has no ``fio_options`` parameter; the settings are
    accepted on the config today and start flowing automatically when the
    installed s3transfer grows the parameter (aws-cli's bundled fork already
    has it).
    """
    import inspect

    if "fio_options" not in inspect.signature(create_fn).parameters:
        return
    fio_options: dict[str, Any] = {}
    should_stream = getattr(config, "should_stream", None)
    if should_stream is not None:
        fio_options["should_stream"] = should_stream
    disk_throughput = getattr(config, "disk_throughput", None)
    if disk_throughput is not None:
        # Bytes per second to gigabits per second, aws-cli's conversion.
        fio_options["disk_throughput_gbps"] = disk_throughput * 8 / 1_000_000_000
    direct_io = getattr(config, "direct_io", None)
    if direct_io is not None:
        fio_options["direct_io"] = direct_io
    if fio_options:
        kwargs["fio_options"] = fio_options


def _reset_for_tests() -> None:  # pyright: ignore[reportUnusedFunction] # test seam
    """Drop the process singletons so tests can exercise initialization."""
    global _crt_s3_client, _crt_serializer
    with _CREATION_LOCK:
        _crt_s3_client = None
        _crt_serializer = None
