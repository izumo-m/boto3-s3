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
client (design/crt.md):

- **endpoint**: boto3 always passes ``endpoint_url=None`` to the serializer,
  which breaks custom endpoints (MinIO et al). We derive the endpoint from
  ``client.meta.endpoint_url`` - kept for custom hosts, ``None`` for the AWS
  default form (recognized across all botocore partitions, not just the two
  commercial suffixes) so botocore re-resolves per request exactly like boto3.
  ``use_ssl`` follows the custom endpoint's scheme (aws-cli only honors an
  ``--endpoint-url`` argument here; deriving from the resolved client also
  covers ``AWS_ENDPOINT_URL_S3`` for non-AWS hosts - an env endpoint under
  an AWS DNS suffix still derives ``None``, the explicit-``--endpoint-url``
  VPC-endpoint caveat).
- **verify / signing**: by default the TLS verification setting is
  recovered from the client (boto3 ignores it), riding on private botocore
  attributes at the same level as ``client._get_credentials()``, which boto3
  itself uses. Whether the CRT client signs at all is derived from the client
  the same way. A caller that owns either decision, as aws-cli's CRT factory
  does with its own ``verify_ssl`` / ``ca_bundle`` and ``sign_request``
  readings, states it in ``CrtClientOptions`` instead (see
  `create_crt_transfer_manager`).
- **compatibility**: the singleton additionally pins the derived endpoint and
  the signed/unsigned mode; a later client that disagrees falls back to
  classic, same as boto3's region/credentials mismatch. The credentials half
  of that check recognizes the credentials object the singleton's delegate
  wraps without resolving it (one session's clients share it), so a
  refreshable credential is first resolved inside the delegate, per request,
  as aws-cli's factory lets it be - boto3's value comparison, kept for a
  client holding another object, would resolve it ahead of the transfer. A
  signing client that resolved no credentials at all never gets the CRT
  engine (boto3's rule), so its transfer runs classic and fails there.

The CRT client's region is the built client's own, as in boto3.

Nothing here imports awscrt or ``s3transfer.crt`` at module import time; the
classic path never pays for them (design/imports.md).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, TypedDict
from urllib.parse import urlsplit

from boto3_s3.types import CrtClientOptions

if TYPE_CHECKING:
    from boto3.session import Session
    from mypy_boto3_s3 import S3Client

    from boto3_s3.crtrequest import CrtRequestSender

logger = logging.getLogger(__name__)

# The package logger's NullHandler (the rule and the reason are at the same
# registration in s3storage): this module is a documented surface reachable
# without s3storage - `from boto3_s3 import crtsupport` and a
# `create_crt_transfer_manager` call whose config the installed s3transfer
# cannot take - so its warning must not fall through to `lastResort` either.
_package_logger = logging.getLogger("boto3_s3")
if not any(isinstance(h, logging.NullHandler) for h in _package_logger.handlers):
    _package_logger.addHandler(logging.NullHandler())

__all__ = [
    "PROCESS_LOCK_NAME",
    "acquire_process_lock",
    "caller_endpoint",
    "create_crt_request_sender",
    "create_crt_transfer_manager",
    "has_crt_s3transfer",
    "has_minimum_crt_version",
    "is_optimized_for_system",
    "materialize_crt_engine",
    "selects_crt",
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


@dataclass(frozen=True)
class _Pins:
    """What the CRT client and the shared serializer bake in at creation.

    None of it can be swapped per request, so a later client rides the
    singleton only when its own pins are equal (`_is_compatible_request`);
    otherwise it falls back to classic rather than transfer under the first
    client's settings - silently reusing the first client's ``verify`` would
    flip TLS verification behind the caller's back. Each facet is compared
    as botocore resolves it, not as the caller spelled it:

    - ``region``: the client's ``region_name``, boto3's own pin.
    - ``endpoint``: the endpoint the CRT wiring dials (`_resolve_endpoint`)
      in `_endpoint_key`'s form.
    - ``verify``: what the CRT client trusts (`_effective_verify`: ``None`` /
      ``False`` / a CA path).
    - ``config_shape``: `_serializer_config_shape`'s tuple, the ``Config``
      facets the shared serializer bakes in (the ``s3`` dict, the fips /
      dualstack endpoint flags).

    The signing mode and the credentials are not pins of this kind: the
    singleton's ``cred_wrapper`` records the mode, and the credentials half
    of the check compares the identity.
    """

    region: str | None
    endpoint: tuple[str, str, str] | None
    verify: Any
    config_shape: tuple[tuple[tuple[str, Any], ...], bool, bool]

    @classmethod
    def of(cls, client: S3Client, endpoint: str | None, crt: CrtClientOptions) -> _Pins:
        """``client``'s pins under the caller's explicit ``endpoint`` and ``crt`` options."""
        return cls(
            region=client.meta.region_name,
            endpoint=_endpoint_key(_resolve_endpoint(client, endpoint)),
            verify=_effective_verify(client, crt.verify),
            config_shape=_serializer_config_shape(client),
        )


class _CrtS3Client:
    """The held CRT client plus the identity it was created for.

    Mirrors boto3's ``CRTS3Client``: the CRT client can only sign for one
    region/credentials (and, in our wiring, one endpoint, signing mode, TLS
    ``verify`` setting, and serializer-relevant ``Config`` shape), so these
    are pinned to reject incompatible later clients: ``pins`` (`_Pins`)
    holds what the CRT client and the shared serializer bake in, and
    ``cred_wrapper`` / ``credentials`` the signing mode and the identity.
    """

    def __init__(
        self,
        *,
        crt_client: Any,
        process_lock: Any,
        pins: _Pins,
        credentials: Any | None,
        cred_wrapper: Any | None,
        serializer_args: tuple[Any, dict[str, Any]] = (None, {}),
    ) -> None:
        self.crt_client = crt_client
        # None = built under the lockless opt-in; `_get_crt_s3_client`
        # upgrades it in place once a lock-respecting request re-acquires.
        self.process_lock = process_lock
        self.pins = pins
        # The botocore credentials object the delegate below wraps (None when
        # the client signs nothing, or resolved none): the identity
        # `_is_compatible_request` recognizes by object, before any resolving.
        self.credentials = credentials
        # None = this CRT client signs nothing (the client resolved as
        # unsigned, or the caller declared `sign_requests=False`), which is
        # also the signing mode `_is_compatible_request` pins on.
        self.cred_wrapper = cred_wrapper
        # The botocore session and client arguments the shared serializer was
        # built from, for `request_sender` to address requests the same way.
        self.serializer_args = serializer_args
        self._sender: Any | None = None
        self._sender_lock = threading.Lock()

    def request_sender(self) -> Any:
        """The `crtrequest.CrtRequestSender` on this CRT client, built on first use."""
        with self._sender_lock:
            if self._sender is None:
                from boto3_s3.crtrequest import CrtRequestSender

                session, client_kwargs = self.serializer_args
                self._sender = CrtRequestSender(self.crt_client, session, client_kwargs)
            return self._sender


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
    host the CRT is tuned for. Separate from ``should_use_crt`` so
    ``transferconfig.resolve_transfer_client`` can port the aws-cli decision
    tree primitive-by-primitive.
    """
    if not has_minimum_crt_version():
        return False
    import awscrt.s3

    return bool(awscrt.s3.is_optimized_for_system())


def acquire_process_lock() -> bool:
    """Claim this process's CRT slot; ``False`` if another process holds it.

    The lock is process-global in ``s3transfer`` (stored and reused), so the
    claim ``transferconfig.resolve_transfer_client`` makes and the later
    ``create_crt_transfer_manager`` acquisition return the same lock.
    """
    from s3transfer.crt import acquire_crt_s3_process_lock

    # Under the creation lock, as `_initialize` acquires it: s3transfer
    # checks for its stored lock and then creates one, so two threads
    # resolving at once would each create one, and the second acquisition in
    # one process fails. Typed as the lock object, but the documented contract
    # returns None when another process holds it (same Any-cast as _initialize).
    with _CREATION_LOCK:
        lock: Any = acquire_crt_s3_process_lock(PROCESS_LOCK_NAME)
    return lock is not None


def should_use_crt(preferred: str) -> bool:
    """boto3's ``_should_use_crt`` over an already-extracted preference.

    Raises botocore's ``MissingDependencyException``, as boto3 does, when
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


def _prefers_crt(config: Any | None) -> bool:
    """Whether *config* names the CRT engine explicitly (``'auto'`` does not)."""
    preferred = getattr(config, "preferred_transfer_client", None)
    if preferred is None:
        preferred = "auto"
    return str(preferred).lower() == "crt"


def selects_crt(config: Any | None) -> bool:
    """Whether *config* selects the CRT engine, with boto3's rule and defaults.

    No config - or one carrying no ``preferred_transfer_client`` - is boto3's
    ``'auto'``. Only ``None`` maps to ``'auto'``: any other value, an empty
    string included, is compared as-is, so an unrecognized preference selects
    classic exactly as boto3's ``_should_use_crt`` (which matches it against
    no branch) decides it. ``'classic'`` short-circuits *before*
    `should_use_crt`, so an explicitly classic run never raises the
    missing-awscrt error an explicit ``'crt'`` does. Shared by the transfer
    engine and `materialize_crt_engine` so the two cannot answer differently.
    """
    preferred = getattr(config, "preferred_transfer_client", None)
    if preferred is None:
        preferred = "auto"
    if str(preferred).lower() == "classic":
        return False
    return should_use_crt(str(preferred))


class _EngineArguments(TypedDict):
    """`create_crt_transfer_manager`'s posture keywords, as `CrtPostures` fills them."""

    endpoint: str | None
    session: Session | None
    crt: CrtClientOptions


@dataclass(frozen=True)
class CrtPostures:
    """A caller's CRT engine postures, carried as one value.

    ``S3`` builds one from its own arguments and hands it to the transfer
    engine (``Transferrer``) and the deletes (`crt_delete_sender`, sync's
    delete lane); ``S3Deleter`` builds one from its public keywords. Each
    field is the keyword of the same name that `create_crt_transfer_manager`
    documents, except ``endpoint``, which is the caller's explicit endpoint
    before `caller_endpoint` keeps it for the client built from it - applied
    by `engine_arguments`, so no holder can pin it onto another client.
    """

    endpoint: str | None = None
    session: Session | None = None
    crt: CrtClientOptions = field(default_factory=CrtClientOptions)

    def engine_arguments(self, client: S3Client) -> _EngineArguments:
        """The keywords this module's engine builders take, for a run on ``client``."""
        return {
            "endpoint": caller_endpoint(client, self.endpoint),
            "session": self.session,
            "crt": self.crt,
        }


def create_crt_transfer_manager(
    client: S3Client,
    config: Any | None,
    *,
    endpoint: str | None = None,
    session: Session | None = None,
    crt: CrtClientOptions | None = None,
) -> Any | None:
    """Return a ``CRTTransferManager`` for ``client``, or ``None`` for classic.

    ``None`` means the boto3-faithful fallbacks fired: the cross-process lock
    is held elsewhere, or the process singleton was created for a different
    region / credentials / endpoint / signing mode / TLS ``verify`` /
    ``Config.s3`` shape.

    ``crt`` is the caller's `CrtClientOptions` (``None`` is its defaults,
    boto3's behavior). ``crt.allow_lockless`` opts out of the lock half of the
    fallback, for an **explicit** ``'crt'`` preference only: aws-cli
    documents that preference as the way to run the CRT client in concurrent
    processes, and its factory builds the CRT client whether or not it got
    the lock, where boto3 - and the default here - silently selects classic.
    An ``'auto'`` preference keeps the lock requirement under this opt-in
    too: that is aws-cli's own ``auto``, which resolves classic when another
    process holds the lock (design/crt.md section 6).

    ``endpoint`` is the caller's explicit endpoint (the CLI threads its
    ``--endpoint-url`` here, matching aws-cli, which passes it to the CRT
    serializer verbatim - so a custom endpoint under an AWS domain, e.g. a VPC
    interface endpoint, is honored rather than re-resolved to public S3). When
    it is ``None`` the host-heuristic default (``_derive_endpoint``) decides,
    which is boto3-faithful for a real AWS endpoint and still pins a non-AWS
    custom host such as MinIO.

    ``session`` is the caller's boto3 session (``S3.session`` threads it via
    the ``Transferrer``); the CRT request serializer is built from it rather
    than a fresh one - see ``_botocore_session`` for why that matters.

    ``crt.sign_requests`` is the caller's signing declaration, aws-cli's
    ``sign_request`` parameter. ``None`` (the default) derives it from the
    client, boto3's rule: an ``UNSIGNED`` client gets no credentials provider
    and everything else gets one. ``False`` builds the CRT client without a
    provider whatever the client says, ``True`` always with one. The
    declaration exists because aws-cli's CRT factory reads only its own
    ``--no-sign-request`` while the per-client ``Config(signature_version=
    's3v4')`` that ``--sse aws:kms`` adds reaches its *botocore* client alone:
    an unsigned KMS run is anonymous on aws-cli's CRT lane and signed on its
    classic lane, and only a declaration can reproduce that split from a
    client that already resolved to signing (design/crt.md section 4).

    ``crt.verify`` is the CRT client's trust source; ``None`` derives it from
    the client (`_effective_verify`).
    """
    # First of all, before the cross-process lock and whatever state the
    # singleton is in: a CA bundle setting that would turn TLS verification
    # off is refused (botocore's own InvalidConfigError, which the transfer
    # engine's seam refines). Deferred: this module loads nothing SDK-backed.
    from boto3_s3.s3storage import reject_empty_ca_bundle

    reject_empty_ca_bundle(client)
    if config is None:
        # CRTTransferManager itself accepts config=None but dereferences it at
        # the first transfer (its args creator reads config.multipart_chunksize),
        # and boto3 always hands the manager a real TransferConfig - so a
        # zero-config caller reaching the CRT lane gets the same defaults the
        # classic lane builds for itself. Deferred import: this module stays
        # free of boto3 at import time.
        from boto3_s3.transferconfig import TransferConfig

        config = TransferConfig()
    options = crt if crt is not None else CrtClientOptions()
    crt_s3_client = _get_crt_s3_client(
        client, config, CrtPostures(endpoint=endpoint, session=session, crt=options)
    )
    if crt_s3_client is None or not _is_compatible_request(
        client, crt_s3_client, endpoint, crt=options
    ):
        return None
    from s3transfer.crt import CRTTransferManager

    kwargs: dict[str, Any] = {
        "crt_s3_client": crt_s3_client.crt_client,
        "crt_request_serializer": _crt_serializer,
    }
    # Back-compat shim (floor s3transfer 0.6.2, docs/compatibility.md):
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


def materialize_crt_engine(
    client: S3Client,
    config: Any | None,
    *,
    endpoint: str | None = None,
    session: Session | None = None,
    crt: CrtClientOptions | None = None,
) -> None:
    """Build the engine *config* selects now, for a caller that will not transfer.

    ``aws s3 rm`` constructs its transfer manager before it knows whether the
    run deletes anything, so every construction-time failure - a CA bundle the
    CRT cannot read, say - surfaces there rather than inside the operation. This library's ``rm``
    builds the engine later - with its deleter, just before the listing, or
    just before a single key's request, and never for a dryrun - so a caller
    owing that order pays the same construction here (cross-process lock
    included) and drops the result; the deletes reuse it.

    A config that selects the classic engine - explicitly, or through ``auto``
    on a host the CRT is not tuned for - makes this a no-op. Nothing is
    returned: the CRT client is a process-wide singleton, so a later transfer
    on a compatible client reuses whatever this built.
    """
    if not selects_crt(config):
        return
    create_crt_transfer_manager(
        client,
        config,
        endpoint=endpoint,
        session=session,
        crt=crt,
    )


def create_crt_request_sender(
    client: S3Client,
    config: Any | None,
    *,
    endpoint: str | None = None,
    session: Session | None = None,
    crt: CrtClientOptions | None = None,
) -> CrtRequestSender | None:
    """The CRT engine's client as a sender of single S3 requests, or ``None``.

    The engine is resolved exactly as `create_crt_transfer_manager` resolves
    it for a transfer - the same singleton, lock, compatibility pins and
    postures, and the same construction-time failures - and ``None`` means
    the same classic fallback. What comes back sends requests s3transfer's
    manager has no route for (`crtrequest`): the deleter's ``DeleteObjects``,
    and with them its single ``DeleteObject`` requests and ``rm``'s.
    """
    if (
        create_crt_transfer_manager(
            client,
            config,
            endpoint=endpoint,
            session=session,
            crt=crt,
        )
        is None
    ):
        return None
    holder = _crt_s3_client
    if holder is None:
        # Unreachable: a manager is only built on a live singleton (and the
        # singleton is never dropped outside the test seam).
        return None
    return holder.request_sender()


def caller_endpoint(client: S3Client, endpoint: str | None) -> str | None:
    """The caller's explicit endpoint, kept only for the client built from it.

    The pin belongs to the client the endpoint produced: pinning an
    application-level endpoint onto a client that came from somewhere else (an
    ``S3Storage`` carrying its own) would dial one host with another's
    credentials and region, where the classic lane simply uses that client.
    botocore stores an explicit ``endpoint_url`` verbatim on ``meta``, so
    equality identifies the right client; any other one falls back to the host
    heuristic on its own endpoint (`_derive_endpoint`).
    """
    if endpoint is not None and client.meta.endpoint_url != endpoint:
        return None
    return endpoint


def _get_crt_s3_client(
    client: S3Client, config: Any | None, postures: CrtPostures
) -> _CrtS3Client | None:
    global _crt_s3_client, _crt_serializer
    with _CREATION_LOCK:
        if _crt_s3_client is None:
            serializer, crt_s3_client = _initialize(client, config, postures)
            _crt_serializer = serializer
            _crt_s3_client = crt_s3_client
        elif _crt_s3_client.process_lock is None and not (
            postures.crt.allow_lockless and _prefers_crt(config)
        ):
            # A singleton built under the lockless opt-in holds no lock, and
            # the opt-in is scoped to explicit-'crt' requests (design/crt.md
            # section 6) - a lock-respecting request keeps aws-cli's own lock
            # semantics by re-attempting the acquisition: s3transfer stores
            # the lock process-globally, so a success upgrades this singleton
            # for every later request, while live contention resolves this
            # run classic instead of riding the opt-in through the singleton.
            from s3transfer.crt import acquire_crt_s3_process_lock

            lock: Any = acquire_crt_s3_process_lock(PROCESS_LOCK_NAME)
            if lock is None:
                return None
            _crt_s3_client.process_lock = lock
    return _crt_s3_client


def _botocore_session(session: Session | None) -> Any:
    """The botocore session the CRT request serializer is built from.

    aws-cli hands its live CLI session to ``BotocoreCRTRequestSerializer``
    (``factory.py``), so the serializer's internal client build hits an
    already-warm loader. A fresh ``botocore.session.Session()`` here instead
    re-reads and re-parses the S3 service model and endpoint data on every
    process - measured at ~40 ms per invocation (2026-07-14), the dominant
    fixed cost of the CRT lane versus aws. Preference order: the caller's
    session, then boto3's default session when one exists (``S3.client()``
    without a session creates it, loader already warm), then a fresh session
    for callers that brought a bare client from elsewhere.
    """
    import boto3

    if session is not None:
        return session._session  # pyright: ignore[reportPrivateUsage]
    if boto3.DEFAULT_SESSION is not None:
        return boto3.DEFAULT_SESSION._session  # pyright: ignore[reportPrivateUsage]
    from botocore.session import Session as BotocoreSession

    return BotocoreSession()


def _initialize(
    client: S3Client, config: Any | None, postures: CrtPostures
) -> tuple[Any, _CrtS3Client] | tuple[None, None]:
    """Acquire the process lock and construct the shared CRT serializer and client."""
    from s3transfer.crt import (
        BotocoreCRTRequestSerializer,
        acquire_crt_s3_process_lock,
        create_s3_crt_client,
    )

    # Annotated Any: s3transfer types the return as the lock object, but the
    # documented contract is "None when another process holds it".
    lock: Any = acquire_crt_s3_process_lock(PROCESS_LOCK_NAME)
    if lock is None and not (postures.crt.allow_lockless and _prefers_crt(config)):
        # Another process of this application holds the CRT client; classic.
        # Under the `allow_lockless` opt-in an *explicit* 'crt' preference
        # builds on regardless (aws-cli's factory ignores its own acquisition
        # result); 'auto' keeps the lock requirement, aws-cli's own auto
        # resolution.
        return None, None

    region = client.meta.region_name
    endpoint_url = _resolve_endpoint(client, postures.endpoint)
    # The caller's client Config rides into the serializer's nested botocore
    # client (the serializer merges signature_version=UNSIGNED on top). Without
    # it, a None endpoint re-resolves under *stock botocore's* defaults, which
    # send us-east-1 to the legacy global endpoint (s3.amazonaws.com) - aws-cli v2's
    # bundled botocore is regional-only there, and the CLI pins the classic
    # engine to regional the same way (its us_east_1_regional_endpoint
    # override, clientfactory), so the CRT lane must serialize with the same
    # Config to keep one Host across engines. Addressing-style and
    # accelerate/dualstack settings carry over for the same reason - pinned,
    # so the serializer's own session cannot fill in what the client's left
    # out (`_serializer_config`).
    botocore_session = _botocore_session(postures.session)
    serializer_kwargs: dict[str, Any] = {
        "region_name": region,
        "endpoint_url": endpoint_url,
        "config": _serializer_config(client),
    }
    # A copy: s3transfer's serializer writes its own entries into the dict.
    serializer = BotocoreCRTRequestSerializer(botocore_session, dict(serializer_kwargs))

    create_kwargs: dict[str, Any] = {
        "region": region,
        "use_ssl": _derive_use_ssl(endpoint_url),
        "verify": _effective_verify(client, postures.crt.verify),
        # aws-cli passes None when multipart_chunksize is not explicit so the
        # CRT picks the part size dynamically; s3transfer's own default is a
        # fixed 8 MiB, hence always passing the key.
        "part_size": _explicit_chunksize(config),
        "target_throughput": getattr(config, "target_bandwidth", None),
    }
    cred_wrapper = _credentials_wrapper(client, postures.crt.sign_requests)
    if cred_wrapper is not None:
        create_kwargs["crt_credentials_provider"] = cred_wrapper.to_crt_credentials_provider()
    # The object the wrapper holds, kept beside it for the identity
    # short-circuit (`_is_compatible_request`); neither resolves it here.
    credentials = _client_credentials(client) if cred_wrapper is not None else None
    crt_client = create_s3_crt_client(**create_kwargs)
    return serializer, _CrtS3Client(
        crt_client=crt_client,
        process_lock=lock,
        pins=_Pins.of(client, postures.endpoint, postures.crt),
        credentials=credentials,
        cred_wrapper=cred_wrapper,
        serializer_args=(botocore_session, serializer_kwargs),
    )


# The `s3` Config facets botocore reads at these values when they are absent:
# a client naming one of them explicitly shapes the same URL as a client that
# leaves it out, so `_serializer_config_shape` drops them before comparing,
# and `_serializer_config` pins every one the client leaves out.
_S3_CONFIG_DEFAULTS: dict[str, Any] = {
    "addressing_style": "auto",
    "use_accelerate_endpoint": False,
    "use_dualstack_endpoint": False,
    "use_arn_region": True,
    "us_east_1_regional_endpoint": "legacy",
    "s3_disable_multiregion_access_points": False,
    "payload_signing_enabled": None,
}


def _serializer_config(client: S3Client) -> Any:
    """The client's ``Config`` as the serializer's own client must be given it.

    The serializer builds a second botocore client to address the CRT's
    requests, and two things ``client.meta.config`` holds would not carry
    over as the transfer client's:

    - ``user_agent``: botocore stores the client's preliminary User-Agent
      string there, and a client handed it takes it for the caller's own
      ``user_agent``, which freezes the header in its legacy form -
      ``app/<id>`` (``sdk_ua_app_id`` / ``AWS_SDK_UA_APP_ID``) and
      ``cfg/retry-mode`` dropped, ``Botocore/<v>`` doubled, where the classic
      requests and aws-cli's CRT ones carry them (measured). What the caller
      supplied, which botocore keeps aside, goes back in its place.
    - what the serializer's session can fill in: a key the ``s3`` dict leaves
      out, a fips / dualstack flag left unset, and an app id the client has
      none of come from *that* session's
      profile, which is not the client's when the client was built from
      another session (``S3(session=X)`` with ``S3Storage(uri, client=<a
      client of Y>)``) - X's ``[s3] addressing_style = virtual`` put the CRT's
      requests on a virtual host where classic's went path-style, and X's
      ``sdk_ua_app_id`` named the CRT's requests where classic's carried none
      (measured). Each is pinned to the client's resolved value, or to
      botocore's own default (`_S3_CONFIG_DEFAULTS`; an empty app id, which
      botocore leaves out of the header).

    The rest of the client's resolved Config - timeouts, retries, checksum
    modes - is recorded on it explicitly and rides as it was. What botocore
    takes from the session rather than the Config - the product name leading
    the header, a session-level ``user_agent_extra`` - is the serializer
    session's; and the engine is process-wide, so a later client sharing it
    sends its CRT requests under the name of the client the engine was built
    for. Only the User-Agent differs that way, which is not a reason to leave
    the engine: the compatibility check pins what shapes the requests
    (`_is_compatible_request`), as boto3's own check pins less.
    """
    from botocore.config import Config

    config: Any = client.meta.config
    client_s3: Mapping[str, Any] = getattr(config, "s3", None) or {}
    # Typed loosely: botocore-stubs declares neither every `s3` key nor
    # `user_agent_appid`.
    pins: dict[str, Any] = {
        "user_agent": getattr(config, "_supplied_user_agent", None),
        "user_agent_appid": getattr(config, "user_agent_appid", None) or "",
        "s3": {**_S3_CONFIG_DEFAULTS, **client_s3},
        "use_fips_endpoint": bool(getattr(config, "use_fips_endpoint", None)),
        "use_dualstack_endpoint": bool(getattr(config, "use_dualstack_endpoint", None)),
    }
    return config.merge(Config(**pins))


# Marks a key `_S3_CONFIG_DEFAULTS` has no default for: its value is always compared.
_NO_DEFAULT: Final = object()


def _serializer_config_shape(client: S3Client) -> tuple[tuple[tuple[str, Any], ...], bool, bool]:
    """The client-Config facets the shared serializer / CRT client bake in.

    The ``s3`` dict (addressing style, accelerate/dualstack, us-east-1
    regional form) plus the top-level fips / dualstack endpoint flags, which
    shape URL resolution outside the ``s3`` dict. Compared by
    `_is_compatible_request` so a later client with different URL-shaping
    settings falls back to classic instead of riding the first client's.

    Compared as what botocore resolves, not as the caller spelled it: a
    missing ``s3`` dict, an empty one and one naming only defaults
    (``addressing_style='auto'``) shape the same URL, and a flag left
    ``None`` reads as ``False`` - taken verbatim, ``Config()`` against
    ``Config(s3={})`` sent the second client to classic for nothing
    (measured). botocore merges the config file's ``[s3]`` keys into the
    dict before it reaches here, so a value that came from the file compares
    like one the caller passed.
    """
    config: Any = client.meta.config
    s3: Mapping[str, Any] = getattr(config, "s3", None) or {}
    explicit = tuple(
        sorted(
            (key, value)
            for key, value in s3.items()
            if _S3_CONFIG_DEFAULTS.get(key, _NO_DEFAULT) != value
        )
    )
    return (
        explicit,
        bool(getattr(config, "use_fips_endpoint", None)),
        bool(getattr(config, "use_dualstack_endpoint", None)),
    )


def _endpoint_key(endpoint: str | None) -> tuple[str, str, str] | None:
    """An endpoint as botocore joins it into a request URL, for the pin.

    botocore lowercases the scheme, matches the host without regard to case
    and drops a trailing slash when it joins the path (measured:
    ``http://127.0.0.1:9000``, ``http://127.0.0.1:9000/`` and
    ``HTTP://127.0.0.1:9000`` all request ``http://127.0.0.1:9000/b/k``), so
    two spellings differing only there dial the same host and share the
    singleton; a path stays (``/base`` is another endpoint).
    """
    if endpoint is None:
        return None
    parts = urlsplit(endpoint)
    return (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"))


def _is_compatible_request(
    client: S3Client,
    crt_s3_client: _CrtS3Client | None,
    endpoint: str | None,
    *,
    crt: CrtClientOptions,
) -> bool:
    """boto3's ``is_crt_compatible_request`` plus the endpoint / signing /
    TLS-``verify`` / Config-shape pins (`_Pins` holds all but the signing mode).

    The last two go beyond boto3's own check (region + credentials): the
    singleton CRT client holds the first client's ``verify`` and the shared
    serializer its ``Config`` (`_serializer_config_shape`: the ``s3`` dict
    plus the fips / dualstack endpoint flags), so a later client differing in
    either would silently transfer under the first one's TLS and URL-shaping
    settings. The User-Agent the serializer names requests with is not
    pinned: a client differing only there shares the engine
    (`_serializer_config`).
    Incompatible means classic for that run, never a wrong-config CRT
    transfer.

    ``crt`` holds the caller's options: its ``verify`` feeds the pins, and
    its ``sign_requests`` must be the same declaration the singleton was
    built under, so the signing pin compares like with like
    (`_signs_requests`): the singleton's ``cred_wrapper`` already records
    the mode it was created in, so a later request whose declaration
    disagrees falls back to classic rather than transferring under the first
    one's signing mode.

    The credentials half compares by object before it compares by value: a
    client whose credentials object is the very one the singleton's delegate
    wraps - every client of one session shares it - has the same identity by
    construction, and nothing is resolved to say so. That matters for a
    refreshable credential (an assumed role, an SSO token): boto3's frozen
    comparison, kept for a client holding another object, resolves it on the
    spot, so a refresh that fails - the token expired, STS unreachable -
    fails the whole run before any transfer, where aws-cli's factory, which
    never resolves ahead of the transfer, lets it fail inside the delegate
    per item and lets a dryrun complete (measured: `cp --dryrun` rc 0 there,
    and the per-item ``AWS_AUTH_CREDENTIALS_PROVIDER_DELEGATE_FAILURE`` on a
    live upload). The resolution a value comparison performs raises whatever
    botocore raises; the engine seams classify it (`transfer.crt_engine_errors`).
    """
    if crt_s3_client is None or _Pins.of(client, endpoint, crt) != crt_s3_client.pins:
        return False
    if not _signs_requests(client, crt.sign_requests):
        return crt_s3_client.cred_wrapper is None
    if crt_s3_client.cred_wrapper is None:
        return False
    boto3_creds = _client_credentials(client)
    if boto3_creds is None:
        # boto3's rule: no credentials means no CRT, so the run drops to
        # classic and fails with botocore's own "Unable to locate credentials".
        return False
    if boto3_creds is crt_s3_client.credentials:
        return True
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
    """boto3's explicit-'crt' validation: classic-only options must be unset.

    A config `transferconfig.resolve_transfer_config` settled on CRT without
    the caller asking for it (``auto``, or ``[s3]``'s choice) is not checked:
    the CRT engine ignores classic-only options there, as boto3's ``auto``
    and aws-cli's CRT manager do.
    """
    if config.preferred_transfer_client != "crt" or not getattr(config, "_crt_requested", True):
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


def _effective_verify(client: S3Client, verify: str | bool | None) -> Any:
    """The CRT client's trust source: ``CrtClientOptions.verify``, else the client's own."""
    return _derive_verify(client) if verify is None else verify


def _derive_verify(client: S3Client) -> Any:
    """Map botocore's verify setting onto ``create_s3_crt_client``'s contract.

    The CRT factory takes ``None`` (the *platform* trust store), ``False``
    (skip verification) or a CA-bundle path - never ``True``, which botocore
    uses for its own default. A client that never named a bundle therefore
    maps to ``None``, exactly as plain boto3 does (it passes no ``verify`` to
    the CRT at all): the library stays boto3-faithful even though the platform
    store is not the certifi bundle the classic engine would have used. A
    caller that wants a file there names it in ``CrtClientOptions.verify``,
    as ``boto3-s3-cli`` does with aws-cli's own resolution.

    An empty or whitespace-only string never arrives here:
    `create_crt_transfer_manager` refuses such a client first
    (`s3storage.reject_empty_ca_bundle`).
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


def _signs_requests(client: S3Client, sign_requests: bool | None) -> bool:
    """Whether the CRT client for this request signs at all.

    ``None`` means the caller declared nothing, so the client answers -
    boto3's rule, where an ``UNSIGNED`` client is the only unsigned one. A
    declared value is the caller's own answer and rides verbatim, which is how
    aws-cli's posture is reproduced: its CRT factory attaches a credentials
    provider on its ``sign_request`` parameter (``--no-sign-request``) alone
    and never looks at the client, so ``--no-sign-request --sse aws:kms``
    transfers anonymously there even though the same run's botocore client
    signs.
    """
    return not _is_unsigned(client) if sign_requests is None else sign_requests


def _client_credentials(client: S3Client) -> Any:
    accessor: Any = client  # boto3/crt.py rides the same private accessor
    return accessor._get_credentials()


def _credentials_wrapper(client: S3Client, sign_requests: bool | None) -> Any | None:
    """The CRT credentials delegate, or ``None`` for a client that signs nothing.

    A declared ``sign_requests=False`` short-circuits before
    ``_client_credentials``, so an unsigned run never resolves credentials at
    all - aws-cli's own behavior, where a credential-less anonymous CRT
    upload succeeds instead of failing inside the delegate.
    """
    if not _signs_requests(client, sign_requests):
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


def _reset_for_tests() -> None:  # pyright: ignore[reportUnusedFunction] # test seam
    """Drop the process singletons so tests can exercise initialization."""
    global _crt_s3_client, _crt_serializer
    with _CREATION_LOCK:
        _crt_s3_client = None
        _crt_serializer = None
