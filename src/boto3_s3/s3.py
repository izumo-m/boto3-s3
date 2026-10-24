"""The boto3-s3 ``S3``: entry point for ``aws s3``-style operations.

``S3`` holds no connection of its own; it is a pure orchestrator. Every
``Location`` argument resolves to a ``Storage``: a local path becomes a
``LocalStorage`` and an ``"s3://..."`` string becomes an ``S3Storage``. The
boto3 client lives on the ``S3Storage``; when its client is omitted it falls
back to ``boto3.client("s3")``. The recommended construction is
``S3(session=boto3_s3.session())`` - the tuned session's clients parse
response timestamps at C speed (`sessions.py`); a zero-config ``S3()``
deliberately keeps plain ``boto3.client("s3")`` semantics, riding
``boto3.DEFAULT_SESSION`` exactly as ``boto3.client`` itself does. What that
session holds never decides whether the fast timestamp parser is installed:
that follows only from which session was passed in (`sessions.py`). To target a custom
endpoint / profile / region (e.g. MinIO) or a second account for S3-to-S3,
pass an explicit ``S3Storage(uri, client=...)`` instead of a bare string.

This module is SDK-backed by declaration: it imports boto3 at module top.
Only the package root's lazy re-export keeps a bare ``import boto3_s3``
SDK-free (design/imports.md).
"""

from __future__ import annotations

import codecs
import functools
import inspect
import os
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Concatenate, Literal, ParamSpec, TypeVar

import boto3
from typing_extensions import Self, Unpack

from boto3_s3 import (
    concurrency,
    crtsupport,
    producers,
    requestparams,
    transferconfig,
    transferplan,
)
from boto3_s3.awsconfig import AwsConfig
from boto3_s3.comparator import (
    Comparator,
    MergedPairFilter,
    PairFilter,
    ParallelFilter,
    SrcOnlyPair,
    SyncPair,
)
from boto3_s3.deleter import S3Deleter, crt_delete_sender, delete_record
from boto3_s3.exceptions import (
    AccessDeniedError,
    BatchError,
    Boto3S3Error,
    CancelledError,
    ConfigurationError,
    InvalidValueError,
    NotFoundError,
    ValidationError,
)
from boto3_s3.iostorage import IOStorage
from boto3_s3.localstorage import (
    LocalStorage,
    is_readable,
    translate_os_error,
)
from boto3_s3.s3storage import (
    CA_BUNDLE_ENVIRONMENT_VETTED,
    S3Storage,
    reject_empty_ca_bundle,
    s3express_unsupported_reason,
)
from boto3_s3.sdkerrors import (
    attribute_failure,
    client_build_error,
    request_failure,
    s3_errors,
    s3_request,
)
from boto3_s3.storage import Location, Storage
from boto3_s3.synclanes import (
    Lane,
    SyncDeletes,
    reject_lane_filter_shapes,
    reject_pair_filter_conflicts,
    resolve_lanes,
    run_sync_pairs,
)
from boto3_s3.transfer import TransferItem, Transferrer, crt_engine_errors
from boto3_s3.types import (
    CancelToken,
    CrtClientOptions,
    FileFilter,
    FileInfo,
    ListingCallback,
    OpOutcome,
    ProgressCallback,
    ResultCallback,
    S3FileInfo,
    TransferOptions,
    TransferType,
    strip_response_metadata,
)

if TYPE_CHECKING:
    # Annotation-only names: mypy_boto3_s3 is a stubs-only distribution, and
    # the rest are used in annotations alone.
    from boto3 import Session
    from boto3.s3.transfer import TransferConfig
    from botocore.config import Config
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_s3.type_defs import WebsiteConfigurationTypeDef

    from boto3_s3.awsclisession import AwsCliAssembly


def rm_filter_root(key: str, *, recursive: bool) -> str:
    """The filter prefix an ``rm`` of ``key`` operates under (aws-cli parity).

    A recursive target is normalized to end with ``/`` (aws-cli
    ``FileFormat.s3_format``): ``rm s3://b/data --recursive`` lists under
    ``data/`` and does not touch ``data-sibling.txt``. A non-recursive key uses
    its parent directory, and a bucket-root target uses ``""``
    (aws-cli ``filters._get_s3_root``). ``--exclude`` / ``--include``
    patterns resolve relative to this prefix, and the recursive listing uses
    it as the ``Prefix``. Always empty or ending in a separator.

    aws-cli joins the pattern onto that parent with ``os.path.join``, which adds
    no separator after a parent that already ends in one, so the parent of
    ``a//b`` is ``a/`` (the entry is matched as ``/b``, not ``b``) and the
    parent of ``/b`` is ``""``; the same rule here keeps a glob's verdict on
    such keys identical to aws-cli's. On Windows a backslash is such a
    separator too: a non-recursive parent ending in one is returned as is,
    and a recursive target typed ending in one keeps its ``/``-terminated
    ``Prefix`` while its entries are shown to the filter with a leading
    ``/`` (`producers.s3_listing_filter`).
    """
    if recursive:
        return f"{key}/" if key and not key.endswith("/") else key
    return producers.single_object_filter_root(key)


# Every key TransferOptions accepts (the TypedDict is total=False, so the
# two frozensets union to the full key set regardless of future totality).
_TRANSFER_OPTION_KEYS = frozenset(TransferOptions.__required_keys__) | frozenset(
    TransferOptions.__optional_keys__
)


def _validate_transfer_options(options: TransferOptions, *, operation: str) -> None:
    """Reject unknown ``**options`` keys and a malformed ``grants`` eagerly.

    Both are pre-pipeline `ValidationError`s. ``Unpack[TransferOptions]``
    already enforces the key set for type-checked callers, but an unchecked
    caller's typo (``dry_run`` for ``dryrun``) would otherwise be silently
    ignored - and on ``mv`` still delete the source. A ``grants`` entry not of
    the form ``permission=principal``, or naming an unknown permission, is
    refused here on every route, where aws-cli finds it only as it maps the
    first upload or copy (`requestparams.validate_grants`).
    """
    unknown = sorted(set(options) - _TRANSFER_OPTION_KEYS)
    if unknown:
        raise ValidationError(
            f"Unknown transfer option(s): {', '.join(unknown)}", operation=operation
        )
    requestparams.validate_grants(options.get("grants"), operation=operation)


@contextmanager
def _attributed_to(operation: str) -> Generator[None, None, None]:
    """Stamp ``operation`` onto an unattributed family error leaving the block.

    A storage-level method knows the location but not the operation that is
    about to use it, so its errors would otherwise carry ``operation=None`` -
    the value the contract reserves for "no single operation was in scope"
    (docs/reference/exceptions.md). Only the operation layer knows the name, so
    it fills it in here: the same exception object is re-raised (traceback and
    ``__cause__`` survive), the name is written only when the error left it
    unset (a backend that already attributed the failure keeps its own), and a
    non-family exception passes through untouched. A caller invoking the
    storage method itself still gets ``operation=None``.
    """
    try:
        yield
    except Boto3S3Error as exc:
        if exc.operation is None:
            exc.operation = operation
        raise


_T = TypeVar("_T")


def _attributed_entries(entries: Iterable[_T], operation: str) -> Iterator[_T]:
    """Yield ``entries``, attributing what *fetching* one raises to ``operation``.

    The pull is the listing's and so the operation's (`_attributed_to`); what
    the consumer then does with the entry - the caller's ``on_entry`` /
    ``on_result``, a storage method the callback calls itself - runs between
    two pulls, outside the attribution, so a failure raised there keeps the
    ``operation`` its raiser gave it. A ``ScanOptions.filter`` is not such a
    consumer: the scan calls it while producing the page, inside the pull,
    so an unnamed family error it lets out is named with the listing's
    (docs/reference/filters.md says so).
    """
    iterator = iter(entries)
    while True:
        with _attributed_to(operation):
            try:
                entry = next(iterator)
            except StopIteration:
                return
        yield entry


def _validate_storage(storage: Storage, *, operation: str) -> None:
    """Run ``Storage.validate`` and attribute its failure to ``operation``.

    Then the SDK-floor gate for S3 Express (`_require_s3express_support`).
    """
    with _attributed_to(operation):
        storage.validate()
    _require_s3express_support(storage, operation=operation)


def _require_s3express_support(storage: Storage, *, operation: str) -> None:
    """Refuse an S3 Express location the installed botocore cannot address.

    A directory bucket or its access point on a botocore that predates it
    (`s3express_unsupported_reason`) is a ``ConfigurationError``. It runs in
    the operations, after the location checks, rather than in
    ``S3Storage.validate`` itself, which the CLI calls ahead of its own usage
    errors for a directory bucket (``mb`` / ``sync`` on one, exit code 252 at
    every version).
    """
    if isinstance(storage, S3Storage):
        reason = s3express_unsupported_reason(storage.bucket)
        if reason is not None:
            raise ConfigurationError(reason, operation=operation, bucket=storage.bucket)


@contextmanager
def _single_request(
    operation: str, *, bucket: str | None = None, key: str | None = None
) -> Generator[None, None, None]:
    """Report whatever one request raised as the single-call operation's failure.

    ``mb`` / ``rb`` / ``website`` / ``presign`` issue one client call and raise
    its category error directly (design/exceptions.md section 4). The capture
    is a request's own (`s3_request`): the boto family translated, anything
    else the call raised from inside botocore - a refreshing
    ``credential_process`` whose program is gone (``FileNotFoundError``) - the
    base ``Boto3S3Error`` carrying it as ``__cause__``, the `request_failure`
    shape the blind single-key ``rm`` applies, and only ``AssertionError``
    passed through: an invariant or a test double's guard (``presign`` reads
    the CRT signer's own one itself). On top of it, a family error the call
    raised unattributed is named after this call (`attribute_failure`).
    """
    with s3_request(operation=operation, bucket=bucket, key=key):
        try:
            yield
        except Boto3S3Error as exc:
            attribute_failure(exc, operation=operation, bucket=bucket, key=key)
            raise


def _emit_result(
    on_result: ResultCallback | None,
    *,
    info: FileInfo,
    storage: S3Storage,
    outcome: OpOutcome,
    error: Boto3S3Error | None = None,
    extra_info: Mapping[str, Any] | None = None,
    src: str | None = None,
) -> None:
    """Hand one ``rm`` record to ``on_result``.

    ``src`` is the listing form ``s3://bucket/key`` unless the caller names
    one: the blind single-key path reports its target as it was written
    (``S3Storage.as_text``), like aws-cli's single-object route.
    """
    if on_result is not None:
        on_result(
            delete_record(
                info,
                outcome,
                src=f"s3://{storage.bucket}/{info.key}" if src is None else src,
                storage=storage,
                error=error,
                extra_info=extra_info,
            )
        )


def _raise_if_cancelled(cancel_token: CancelToken | None, operation: str) -> None:
    if cancel_token is not None and cancel_token.cancelled:
        raise CancelledError(f"{operation} was cancelled", operation=operation)


@contextmanager
def _scan_teardown(entries: object, *, reusable_after_interrupt: bool) -> Generator[None]:
    """Close a consumed scan on exit, honoring the process-fatal posture.

    ``ls`` / ``rm`` consume a scan outside `prefetch`'s own pull, so a
    ``KeyboardInterrupt`` landing in their loop bodies (``on_entry``, a
    deleter submission wait) never unwinds through the prefetch context -
    the scan's teardown arrives as a plain ``GeneratorExit``, which always
    joins the page worker. Under ``reusable_after_interrupt=False`` that join is
    exactly what the posture forbids, so the interrupt is marked
    (`concurrency.mark_interrupt_unwinding`) *before* the close runs and the
    teardown abandons the worker instead.

    The caller must iterate a scan it bound to a local, never a bare
    ``for info in storage.scan(...)`` temporary: an exception unwinding the
    frame finalizes an unreferenced temporary while it clears the loop's
    stack - before any handler runs - so the worker would be joined ahead of
    the mark.
    """
    try:
        yield
    except KeyboardInterrupt:
        if not reusable_after_interrupt:
            concurrency.mark_interrupt_unwinding()
        raise
    finally:
        close = getattr(entries, "close", None)
        if close is not None:
            close()


@contextmanager
def _producer_teardown(
    *producers: Generator[object, None, None], reusable_after_interrupt: bool
) -> Generator[None]:
    """Close a transfer run's producers on exit, honoring the process-fatal posture.

    ``cp`` / ``mv`` (`S3._run_transfer`) and ``sync`` (its two side scans)
    close their producers - joining their scan prefetch workers - on every
    exit but one: under ``reusable_after_interrupt=False`` a
    ``KeyboardInterrupt`` skips the close, since the unwind must not wait on
    an in-flight page pull (`S3` docstring), and marks the unwind instead
    (`concurrency.mark_interrupt_unwinding`). The interrupt may have landed
    outside the scan's own pull (a submission wait, a result callback), so
    the producers' eventual teardown - the interpreter finalizes them once
    the traceback holding the caller's frame is released - arrives as a plain
    ``GeneratorExit``; marked, it abandons the page worker instead of joining
    it. Producers close in the order given.

    `_scan_teardown` (``ls`` / ``rm``) closes after marking instead, which
    the mark turns into an abandon at once; folding the two would move these
    producers' teardown on a fatal Ctrl-C from the traceback's release into
    the unwind itself, so they stay apart. Where the close lands relative to
    the engine's own exit is the caller's placement.
    """
    interrupted = False
    try:
        yield
    except KeyboardInterrupt:
        interrupted = True
        if not reusable_after_interrupt:
            concurrency.mark_interrupt_unwinding()
        raise
    finally:
        if reusable_after_interrupt or not interrupted:
            for producer in producers:
                producer.close()


def _is_folder_marker(info: FileInfo) -> bool:
    """A zero-byte ``/``-terminated key - the manual-folder convention."""
    return info.size == 0 and info.key.endswith("/")


def _check_local_source_exists(storage: LocalStorage, *, operation: str) -> None:
    """aws-cli's ``_validate_path_args`` missing-source check, run up front.

    (Their bare RuntimeError -> rc 255; ``NotFoundError`` without a
    ``ClientError`` cause maps the same.)
    """
    # exists() asks the kernel about the path as typed, anchored at the
    # construction-time cwd (so the check survives a chdir): what aws-cli's
    # os.path.exists on the raw path admits - including `..` through a
    # symlinked directory, which the lexical abspath would refuse - ours
    # admits (a single source; a recursive one is then vetted by
    # `_check_local_source_root`), and the generator warns like aws-cli's. The message keeps
    # the raw form, like `aws` echoing the user-typed path.
    if not storage.exists():
        raise NotFoundError(
            f"The user-provided path {storage.path} does not exist.", operation=operation
        )


def _check_local_source_root(storage: LocalStorage, *, operation: str) -> None:
    """Refuse a directory source whose root the walk would skip, before the run.

    A recursive ``cp`` / ``mv`` or a ``sync`` walks a local source as a
    directory, and the walk vets its root first (aws-cli's
    ``should_ignore_file`` on the root with a trailing separator): a file
    (``file/`` does not exist), a special or unreadable path, or a symlink
    under ``follow_symlinks=False``. aws-cli skips such a root - with a
    warning, or silently for the symlink - and runs on an empty source, so a
    ``sync`` with deletes then empties the destination (measured on ``aws``
    2.36.40 under ``--dryrun``: a file source warns and exits 2, a symlink
    root exits 0). Here the run stops before it starts instead, like a missing
    source (`_check_local_source_exists`), and nothing is listed, sent or
    deleted. The walker's own ``should_ignore_file`` decides, so a custom
    walker's rule holds here as in its walk; the complete view
    (``enumerate_all_entries``) vets no root and is left to its walk. The
    error names why: ``AccessDeniedError`` for an unreadable directory,
    ``NotFoundError`` otherwise (the CLI's 255 either way).
    """
    options = storage.default_scan_options()
    if options.enumerate_all_entries:
        return
    reasons: list[str] = []
    # The walker takes the OS form (`LocalStorage.os_path`; the source exists,
    # so its name is one the encoding represents) and names it in its
    # warnings, which read here as text again.
    path = storage.os_path(storage.abspath)
    if not storage.walker.should_ignore_file(
        path + os.sep, follow_symlinks=options.follow_symlinks, notify=reasons.append
    ):
        return
    reasons = [storage.text_path(reason) for reason in reasons]
    typed = storage.path
    if not options.follow_symlinks and os.path.islink(path):
        raise NotFoundError(
            f"The user-provided path {typed} is a symbolic link, which is not followed.",
            operation=operation,
        )
    if not os.path.lexists(path):
        # The kernel found the path as typed (`_check_local_source_exists`),
        # the walk's absolutized form names nothing: a `..` after a symlinked
        # directory.
        raise NotFoundError(
            f"The user-provided path {typed} does not exist as {storage.abspath}.",
            operation=operation,
        )
    if not os.path.isdir(path):
        raise NotFoundError(
            f"The user-provided path {typed} is not a directory.", operation=operation
        )
    if not is_readable(path, True):
        raise AccessDeniedError(
            f"The user-provided path {typed} is not readable.", operation=operation
        )
    detail = " ".join(reasons) if reasons else "The walk skips it."
    raise NotFoundError(
        f"The user-provided path {typed} cannot be walked. {detail}", operation=operation
    )


def _classify_transfer_route(
    plan: transferplan.TransferPlan, *, operation: str
) -> tuple[TransferType, S3Storage, S3Storage | None, str]:
    """Map a plan's route onto the engine wiring cp/mv and sync share.

    Returns ``(transfer_type, client_provider, source_client_provider,
    dest_bucket)``: the providers are the S3 side(s) whose ``get_client()``
    drives s3transfer (``source_client_provider`` only on the s3s3 route) -
    handed back unresolved so each caller keeps its established effect order:
    its own refusals (sync's directory bucket) run before ``get_client()``
    resolves, which *builds* a client only for a caller-constructed
    ``S3Storage`` without one (a ``resolve()``-made storage already carries a
    client built at resolve time), and its destination-directory creation
    runs last of the set-up, once every check has passed.
    The built-in routes assert the concrete ``LocalStorage`` / ``S3Storage``
    pair, because the engine reaches into ``S3Storage``'s client/bucket and
    ``LocalStorage``'s path directly; an open route pairs a capability-checked
    custom backend with S3. The missing-local-source check runs here
    (identical in cp/mv and sync), and for a directory source (a recursive
    cp / mv, a sync) the check of the root the walk would skip
    (`_check_local_source_root`); the destination-directory creation does
    not - the same `LocalStorage.ensure_directory` call, but gated on
    ``recursive`` for cp / mv and unconditional for sync, so it stays with
    the callers.
    """
    src_storage = plan.src
    dest_storage = plan.dest
    if plan.paths_type == "locals3":
        assert isinstance(src_storage, LocalStorage) and isinstance(dest_storage, S3Storage)
        _check_local_source_exists(src_storage, operation=operation)
        if plan.dir_op:
            _check_local_source_root(src_storage, operation=operation)
        return TransferType.UPLOAD, dest_storage, None, dest_storage.bucket
    if plan.paths_type == "s3local":
        assert isinstance(src_storage, S3Storage) and isinstance(dest_storage, LocalStorage)
        return TransferType.DOWNLOAD, src_storage, None, ""
    if plan.paths_type == "s3s3":
        assert isinstance(src_storage, S3Storage) and isinstance(dest_storage, S3Storage)
        return TransferType.COPY, dest_storage, src_storage, dest_storage.bucket
    if plan.paths_type == "opens3":
        # Custom source -> S3: upload each entry from its Storage.open("rb").
        assert isinstance(dest_storage, S3Storage)
        return TransferType.UPLOAD, dest_storage, None, dest_storage.bucket
    # s3open: S3 -> custom destination, download into its open("wb").
    assert isinstance(src_storage, S3Storage)
    return TransferType.DOWNLOAD, src_storage, None, ""


def _bucket_tags(
    tags: Sequence[tuple[str, str]] | Mapping[str, str] | None,
) -> list[dict[str, str]]:
    """Normalize ``S3.mb``'s ``tags`` to the ``CreateBucketConfiguration.Tags`` shape.

    A sequence of pairs passes through in order, duplicates included - the
    server enforces key uniqueness, and the CLI's repeated ``--tags`` relies
    on that rejection for parity. A mapping is the convenience form.
    """
    if tags is None:
        return []
    pairs = tags.items() if isinstance(tags, Mapping) else tags
    return [{"Key": key, "Value": value} for key, value in pairs]


class S3:
    """Entry point for ``aws s3``-style operations (``cp`` / ``ls`` / ``mv`` / ...).

    ``S3`` is a stateless orchestrator: it carries optional defaults
    (``session`` / ``endpoint_url`` / ``config`` for building clients,
    ``transfer_config`` for ``cp`` / ``mv`` / ``sync``) but holds no connection,
    so it needs no cleanup. The transfer ops (``cp`` / ``mv`` / ``sync``) resolve
    each ``Location`` argument by `resolve` - an ``"s3://..."`` string becomes an
    ``S3Storage`` carrying a client from `client`, anything else a
    ``LocalStorage``. The S3-only ops (``ls`` / ``rm`` / ``mb`` / ``rb`` /
    ``presign`` / ``website``) resolve their single target more leniently and do
    not pass through `resolve`: the ``s3://`` scheme is optional and a non-s3
    string is never a local fallback. Either way a bare string inherits these
    defaults (its ``S3Storage`` carries `client`), while a ``Storage`` instance
    passed in is used verbatim (its own client).

    Those defaults are boto3's for everything the caller leaves out (the
    profile, the region, the retries, the trust store, the default checksum).
    `from_aws_cli` builds an instance that answers each the way ``aws s3``
    does instead, from the same options and environment.

    `client` and `resolve` are the customization seams - subclass and
    override ``client`` to change credentials / session / endpoint (or return a
    test double), or ``resolve`` to interpret new URL schemes (e.g. ``http://``).
    Overriding ``resolve`` reaches the transfer ops only; the S3-only ops bypass
    it (they always read their target as S3), so their endpoint / credentials are
    customized through ``client`` alone. A second account for S3-to-S3, or a
    per-location client, is expressed by an explicit ``S3Storage(uri,
    client=...)``. Debug-log secret masking is configured via
    `boto3_s3.set_stream_logger`. The instance's own state
    is thread-safe to share, but its clients need care: ``client()`` is not safe
    to call concurrently (boto3's session construction path), and boto3-s3 uses
    ``s3transfer``, whose per-transfer setup is not thread-safe on a shared
    client. To run operations in parallel across threads, build one client per
    thread sequentially, up front, then give each thread its own
    ``S3Storage(uri, client=...)`` - never share a client, never build clients
    concurrently.

    ``reusable_after_interrupt`` declares the application's Ctrl-C posture, once, for
    every operation this instance starts a scan for - ``ls``, ``rm``, the
    ``cp`` / ``mv`` transfers and ``sync``. The operations with no enumeration
    to reclaim (``mb`` / ``rb`` / ``presign`` / ``website``, and the streaming
    ``cp`` route) never consult it. ``True`` (the default): a
    ``KeyboardInterrupt`` propagates only after the operation has reclaimed
    all of its resources (enumeration workers joined), so an app that catches
    the interrupt can keep calling operations. ``False``: the app treats
    Ctrl-C as process-fatal, so the unwind may abandon a scan's daemon
    prefetch worker instead of waiting out an in-flight listing page pull
    (the CLI's setting - ``aws`` dies immediately on Ctrl-C). Either way the
    interrupt itself is re-raised, never converted or swallowed - on the CRT
    engine too, whose manager would discard one landing in its transfer
    drain (design/crt.md section 6). The posture scopes to
    ``KeyboardInterrupt`` alone - ``SystemExit`` and every other exception
    always get the full reclamation (``sys.exit()``
    requests an *orderly* termination). The posture reaches the scans through
    ``ScanOptions.reusable_after_interrupt``.

    ``crt`` is the CRT transfer engine's own client settings
    (`CrtClientOptions`): whether an explicit ``'crt'`` preference may run
    without the cross-process lock, whether the engine signs, and what it
    trusts for TLS. ``None`` (the default) keeps boto3's behavior, which
    derives each from the client in hand. They reach the transfers and the
    deletes this instance runs on that engine.

    ``filename_encoding`` is handed to every ``LocalStorage`` `resolve` builds
    from a string: the encoding of local file names on POSIX (``None``, the
    default, is Python's file system encoding; it has no effect on Windows).
    See ``LocalStorage`` for what it changes; an unknown codec is an
    ``InvalidValueError`` here. A ``LocalStorage`` passed in keeps its own.
    """

    def __init__(
        self,
        session: Session | None = None,
        *,
        endpoint_url: str | None = None,
        config: Config | None = None,
        transfer_config: TransferConfig | None = None,
        reusable_after_interrupt: bool = True,
        crt: CrtClientOptions | None = None,
        filename_encoding: str | None = None,
    ) -> None:
        self._session = session
        self._endpoint_url = endpoint_url
        self._config = config
        self._transfer_config = transfer_config
        self._reusable_after_interrupt = reusable_after_interrupt
        self._crt = crtsupport.CrtPostures(
            endpoint=endpoint_url,
            session=session,
            crt=crt if crt is not None else CrtClientOptions(),
        )
        # Checked here, as LocalStorage checks it: an unknown codec fails the
        # construction rather than the first local path resolved.
        if filename_encoding is not None:
            try:
                codecs.lookup(filename_encoding)
            except LookupError as exc:
                raise InvalidValueError(str(exc)) from exc
        self._filename_encoding = filename_encoding
        # Memoized AwsConfig (aws_config()): resolve+parse the config file once
        # per instance, since a sync filter may consult it per object. A benign,
        # idempotent cache - concurrent first calls recompute the same reader.
        # Without a session it is kept with the default session it was read
        # under, and read again once boto3's default session changes.
        self._aws_config: tuple[AwsConfig, object] | None = None
        # The aws-cli assembly `from_aws_cli` built this instance on, or None
        # for one the caller assembled (boto3's defaults).
        self._assembly: AwsCliAssembly | None = None

    @classmethod
    def from_aws_cli(
        cls,
        *,
        profile: str | None = None,
        region: str | None = None,
        endpoint_url: str | None = None,
        no_verify_ssl: bool = False,
        ca_bundle: str | None = None,
        no_sign_request: bool = False,
        cli_read_timeout: int | None = None,
        cli_connect_timeout: int | None = None,
        cache_credentials: bool = True,
        transfer_config: TransferConfig | None = None,
        reusable_after_interrupt: bool = True,
    ) -> Self:
        """An ``S3`` whose session and clients are assembled the way ``aws s3`` assembles its own.

        Each keyword is the snake_case form of the ``aws`` common option of the
        same name and means what that option means; one left out is resolved
        as ``aws`` resolves the option's absence - from the environment
        variables and the config file it reads, in its order: the profile from
        ``AWS_PROFILE`` before ``AWS_DEFAULT_PROFILE``, the region from
        ``AWS_REGION``, ``AWS_DEFAULT_REGION``, the profile and the EC2
        instance metadata. The timeouts are seconds, ``0`` meaning no timeout
        and ``None`` botocore's 60. What ``aws``'s bundled botocore does
        differently from the installed one in a normal run is done its way
        too: retries in ``standard`` mode with three attempts, us-east-1 on its
        regional endpoint, CRC64NVME as the default request checksum where
        botocore can compute it (an upload names it in its transfer options).
        The CRT engine's client gets ``aws``'s trust source (``no_verify_ssl``,
        ``ca_bundle``, the config's ``ca_bundle``, botocore's bundled CA file)
        and builds without the cross-process lock under an explicit ``'crt'``
        preference, as ``aws``'s does (`CrtClientOptions`); local file names
        are read and written in the encoding ``aws`` uses in the same
        environment. ``cache_credentials`` keeps the temporary credentials an
        assumed role, a web identity or SSO fetches in aws-cli's own
        ``~/.aws/cli/cache``, which ``aws`` reads too (``False``: in memory,
        as boto3 does). ``transfer_config`` and ``reusable_after_interrupt``
        are ``S3()``'s.

        The session is opened now: a profile that does not exist or a config
        file botocore cannot read raises `InvalidConfigError` here, and a
        schemeless ``endpoint_url`` `ValidationError`, as ``aws`` stops before
        a command runs. A CRT run with no region resolved raises
        `ConfigurationError` before it starts, dry runs included, as ``aws``
        does. Nothing beyond this instance's session is changed: for the
        CONNECT request ``aws`` sends behind an HTTPS proxy, which is
        process-wide, see `pin_proxy_connect`. The session is `session`;
        `client` builds from it.
        """
        from boto3_s3.awsclisession import AwsCliAssembly, AwsCliSettings
        from boto3_s3.localencoding import filename_encoding

        assembly = AwsCliAssembly(
            AwsCliSettings(
                profile=profile,
                region=region,
                endpoint_url=endpoint_url,
                no_verify_ssl=no_verify_ssl,
                ca_bundle=ca_bundle,
                no_sign_request=no_sign_request,
                cli_read_timeout=cli_read_timeout,
                cli_connect_timeout=cli_connect_timeout,
                cache_credentials=cache_credentials,
            )
        )
        s3 = cls(
            session=assembly.session,
            endpoint_url=endpoint_url,
            transfer_config=transfer_config,
            reusable_after_interrupt=reusable_after_interrupt,
            crt=CrtClientOptions(allow_lockless=True, verify=assembly.crt_verify()),
            filename_encoding=filename_encoding(),
        )
        s3._assembly = assembly
        return s3

    @property
    def session(self) -> Session | None:
        """The session that supplies this instance's default clients and AWS config."""
        return self._session

    @property
    def reusable_after_interrupt(self) -> bool:
        """The Ctrl-C posture declared at construction (see the class docstring)."""
        return self._reusable_after_interrupt

    def client(self) -> S3Client:
        """Build a boto3 S3 client from this instance's defaults (the factory seam).

        A fresh client each call, owned by the caller - ``S3`` keeps no
        connection, hence no ``close()``. Built from ``session`` (or boto3's
        default session) with ``endpoint_url`` / ``config`` applied. Override to
        change credentials, reuse a cached client, or return a test double; reuse
        one explicitly via ``S3Storage(uri, client=s3.client())``. A failed build
        raises the translated ``Boto3S3Error`` - ``InvalidConfigError`` for a
        set-but-unusable ``AWS_PROFILE``, partial credentials, a malformed
        region or ``endpoint_url``, a config value of the wrong shape
        (``s3 = foo``), a ``credential_process`` executable that cannot run or
        botocore's own ``InvalidConfigError`` (a ``source_profile`` loop, an
        ``account_id_endpoint_mode`` it does not know), and the base
        ``Boto3S3Error`` for any other
        botocore error, which the translator's last clause claims - never the
        raw botocore error (design/exceptions.md section 1). A client whose
        CA bundle setting resolves to an empty or whitespace-only string is
        refused with ``InvalidConfigError`` rather than returned with TLS
        verification off (`reject_empty_ca_bundle`). Missing
        credentials and an unset region do not fail the build: missing
        credentials fail when the client first signs - a request, or a
        ``presign`` - as a ``ConfigurationError``, and an unset region falls
        back to botocore's default for S3. An instance `from_aws_cli` built
        builds the way ``aws`` does instead, failing as `from_aws_cli` does.
        """
        if self._assembly is not None:
            return self._assembly.client("s3")
        # Outside the translation: a session without `client` is the
        # caller's programming error, and passes through as one.
        build: Any = self._session.client if self._session is not None else boto3.client
        # operation=None: no subcommand is in scope at build time.
        with s3_errors(operation=None):
            try:
                client = build("s3", endpoint_url=self._endpoint_url, config=self._config)
            except (ValueError, AttributeError, OSError) as exc:
                # What the SDK raises outside its own family while it reads
                # the configuration (`client_build_error`). A `config` that is
                # not a botocore Config is the caller's error, not the
                # configuration's.
                from botocore.config import Config as BotocoreConfig

                if isinstance(exc, AttributeError) and not isinstance(
                    self._config,
                    (BotocoreConfig, type(None)),  # pyright: ignore[reportUnnecessaryIsInstance] - a caller's value at runtime
                ):
                    raise
                raise client_build_error(exc) from exc
            reject_empty_ca_bundle(client)
        # Its own value answers for the environment (`reject_empty_ca_bundle`),
        # so the S3Storage it is handed to need not guess at it again. A
        # session that returns an object refusing new attributes just leaves
        # that guess in place.
        with suppress(AttributeError):
            setattr(client, CA_BUNDLE_ENVIRONMENT_VETTED, True)
        return client

    def resolve_transfer_config(
        self, transfer_config: TransferConfig | None = None, *, copy: bool = False
    ) -> TransferConfig:
        """The transfer config a ``cp`` / ``mv`` / ``sync`` / ``rm`` run uses.

        *transfer_config* (this instance's ``transfer_config`` when none is
        given) supplies the settings the caller set; the others come from the
        profile's ``[s3]`` section of this instance's AWS config file, then
        from ``aws s3``'s defaults, and the engine is picked as ``aws s3``
        picks it - always classic for an S3-to-S3 *copy*
        (`transferconfig.resolve_transfer_config`). The operations call this
        themselves; an application calls it to hand the same config to
        `materialize_crt_engine` and the run. A config this returned passes
        through it unchanged, and so does a plain boto3 ``TransferConfig``.

        Raises ``InvalidConfigError`` for an ``[s3]`` value ``aws s3`` would
        reject, and ``ConfigurationError`` when the CRT engine is asked for
        explicitly but its awscrt or s3transfer is missing or too old.
        """
        config = transfer_config if transfer_config is not None else self._transfer_config
        return transferconfig.resolve_transfer_config(self.aws_config(), config, copy=copy)

    def materialize_crt_engine(
        self, client: S3Client, *, transfer_config: TransferConfig | None = None
    ) -> None:
        """Build the CRT transfer engine now, for an operation that will not use it.

        ``aws s3 rm`` constructs its transfer manager before deciding anything
        about the run, so the engine's construction-time failures - a CA
        bundle the CRT cannot read, say - belong to the command, not to the
        deletes. `rm` here builds the engine later - with its deleter,
        just before the listing, or just before a single key's request, and
        never for a ``dryrun`` - so an application that owes ``aws s3`` parity
        calls this to pay the same construction (cross-process lock included)
        at the same point; the deletes then reuse what it built.

        The engine is chosen from *transfer_config* the way the transfer ops
        choose it (`resolve_transfer_config`), so a classic selection -
        explicit, or ``auto`` on a host the CRT is not tuned for - makes this a
        no-op. Nothing is returned: the CRT client is a process-wide singleton
        and a later transfer on a compatible client reuses it.
        """
        config = self.resolve_transfer_config(transfer_config)
        self._require_crt_region(config)
        if not crtsupport.selects_crt(config):
            # The no-op a classic selection makes of the call below, taken
            # here so a plain boto3 config's selection - and the
            # MissingDependencyException an explicit 'crt' without a usable
            # awscrt raises from it - stays outside the classifying seam
            # below, which would turn that pass-through into a
            # ConfigurationError.
            return
        # Construction failures classified exactly as the transfer engine's
        # seam classifies them (`transfer.crt_engine_errors`); no operation is
        # in scope here, so the errors carry none.
        with crt_engine_errors(operation=None):
            # A client the caller hands over (`reject_empty_ca_bundle`).
            reject_empty_ca_bundle(client, check_environment=True)
            crtsupport.materialize_crt_engine(client, config, **self._crt.engine_arguments(client))

    def _run_transfer_config(
        self,
        transfer_config: TransferConfig | None,
        src_storage: Storage,
        dest_storage: Storage,
        *,
        operation: str,
    ) -> TransferConfig:
        """`resolve_transfer_config` for a run between these two sides."""
        copy = isinstance(src_storage, S3Storage) and isinstance(dest_storage, S3Storage)
        with _attributed_to(operation):
            config = self.resolve_transfer_config(transfer_config, copy=copy)
            self._require_crt_region(config)
        return config

    def _require_crt_region(self, config: TransferConfig) -> None:
        """Stop a CRT run of a `from_aws_cli` instance that resolved no region.

        aws-cli hands its region chain's answer to the CRT client, and with
        none its CRT run stops before it starts, a dry run included - where
        botocore would give the client the ``aws-global`` pseudo-region and the
        CRT would sign every request for it. botocore's own missing-region
        error is the cause, so the report is aws-cli's ``NoRegion`` one. An
        instance the caller assembled keeps botocore's answer.
        """
        if self._assembly is None or self._assembly.region is not None:
            return
        if not crtsupport.selects_crt(config):
            return
        from botocore.exceptions import NoRegionError

        cause = NoRegionError()
        raise ConfigurationError(str(cause)) from cause

    def _upload_options(
        self,
        options: TransferOptions,
        src_storage: Storage,
        dest_storage: Storage,
        config: TransferConfig,
    ) -> TransferOptions:
        """*options* with aws-cli's default request checksum for an upload run.

        Only on an instance `from_aws_cli` built, only for a run into S3 from
        anything but S3, and only when the caller named no
        ``checksum_algorithm``: the default aws-cli's bundled botocore would
        stamp, chosen by the engine *config* selects
        (`awsclisession.default_upload_algorithm`). An instance the caller
        assembled leaves each engine to botocore's own default.
        """
        if (
            self._assembly is None
            or "checksum_algorithm" in options
            or not isinstance(dest_storage, S3Storage)
            or isinstance(src_storage, S3Storage)
        ):
            return options
        from boto3_s3.awsclisession import default_upload_algorithm

        algorithm = default_upload_algorithm(
            dest_storage.get_client(), crt=crtsupport.selects_crt(config)
        )
        if algorithm is None:
            return options
        with_default = TransferOptions(**options)
        with_default["checksum_algorithm"] = algorithm
        return with_default

    def resolve(self, loc: Location) -> Storage:
        """Resolve a ``Location`` to a ``Storage`` (the URL-interpretation seam).

        A ``Storage`` instance is returned unchanged; an ``"s3://..."`` string
        becomes an ``S3Storage`` carrying `client`; anything else is a
        ``LocalStorage`` with this instance's ``filename_encoding`` (aws-cli's
        rule: only ``s3://`` is special). Override to
        add schemes such as ``http://``, deferring the rest to
        ``super().resolve(loc)``.

        The string is read the way ``aws s3 cp`` / ``mv`` / ``sync`` read their
        arguments, after ``_normalize_s3_trailing_slash``
        (``S3Storage.normalize_s3_uri``): a keyless URI is the bucket root.
        That changes the location for one spelling - an Outposts access point
        ARN set off with ``:`` and no key (``.../accesspoint/ap:``), which aws-cli
        reads as the key ``/``.
        """
        if isinstance(loc, Storage):
            return loc
        text = os.fspath(loc)
        if text.startswith("s3://"):
            return S3Storage(S3Storage.normalize_s3_uri(text), client=self.client())
        return LocalStorage(text, filename_encoding=self._filename_encoding)

    def aws_config(self) -> AwsConfig:
        """Read this instance's AWS config file (``~/.aws/config``).

        Returns an `AwsConfig` reader over the config
        file resolved for this ``S3``'s ``session`` (or a default
        ``AWS_PROFILE``-aware session when none was given - the same resolution
        `client` uses). The transfer operations read the profile's ``[s3]``
        section through it (`resolve_transfer_config`); use it to surface any
        other profile setting the application did not set itself::

            region = s3.aws_config().get_str("region")

        The reader is memoized per instance (the file is parsed once in the
        common case; concurrent first calls may benignly recompute - see
        ``__init__``). Without a ``session`` it follows
        ``boto3.DEFAULT_SESSION`` as `client` does: a
        ``boto3.setup_default_session(profile_name=...)`` made after a first
        read is picked up by the next one, rather than leaving the reader on
        the profile the clients no longer use.
        """
        source = self._session if self._session is not None else boto3.DEFAULT_SESSION
        memo = self._aws_config
        if memo is None or memo[1] is not source:
            memo = (AwsConfig.from_session(self._session), source)
            self._aws_config = memo
        return memo[0]

    # -- listing ----------------------------------------------------------

    def ls(
        self,
        target: Location = "s3://",
        *,
        on_entry: ListingCallback,
        recursive: bool = False,
        request_payer: str | None = None,
        bucket_name_prefix: str | None = None,
        bucket_region: str | None = None,
        cancel_token: CancelToken | None = None,
    ) -> None:
        """List objects and common prefixes under an S3 ``target``, or all buckets.

        ``ls`` is S3-only (aws-cli parity): ``target`` is an ``"s3://..."`` URI
        string (the ``s3://`` scheme is optional) or an ``S3Storage``. A target
        with no bucket - the default bare ``"s3://"`` - is the service root:
        ``ls`` dispatches to `S3Storage.list_buckets` (aws-cli's ``ls``
        splits the bucket listing from the object listing the same way), yielding
        one ``BUCKET``-kind entry per bucket (``mtime`` = creation date).
        ``bucket_name_prefix`` / ``bucket_region`` filter *that* bucket listing
        and are meaningless for an object listing; each requires a botocore
        whose ``ListBuckets`` can carry it, and one that cannot raises
        ``ValidationError`` before any request (`S3Storage.list_buckets`);
        conversely ``recursive`` /
        ``request_payer`` are meaningless at the service root (both ignored, like
        aws-cli). The listing page size (object listings, and bucket listings on a
        paginator-capable botocore - the floor's fallback issues one
        unpaginated call) is the
        ``S3Storage``'s own ``page_size`` config (its constructor) - pass a
        configured ``S3Storage`` as ``target`` to tune it. A non-S3 ``Location``
        raises ``ValidationError``. The target is validated eagerly. Entries are
        delivered in listing order to `on_entry` on the calling thread.

        `cancel_token` may be cancelled from `on_entry` or another thread.
        Cancellation stops entry delivery, drops prefetched pages, waits for a
        page request already in progress, reclaims the prefetch worker (the
        object listing's machinery; the service-root bucket listing iterates
        its pages directly, so cancellation there just stops between pages),
        and then
        raises `CancelledError`. Both cancellation modes therefore have the
        same effect for listing: synchronous S3 page requests cannot be aborted
        safely once started.
        """
        storage = self._resolve_s3_target(target, operation="ls")
        _raise_if_cancelled(cancel_token, "ls")
        # A lazily-deferred client is built here, ahead of the listing's
        # attribution: a construction failure belongs to no operation
        # (``operation`` None, docs/reference/exceptions.md), as on the
        # transfer routes.
        storage.get_client()
        if not storage.bucket:
            items = storage.list_buckets(name_prefix=bucket_name_prefix, region=bucket_region)
        else:
            items = storage.scan(
                replace(
                    storage.default_scan_options(),
                    recursive=recursive,
                    request_payer=request_payer,
                    reusable_after_interrupt=self._reusable_after_interrupt,
                ),
                cancel_token=cancel_token,
            )
        # The listing is the storage's, which cannot know the operation reading
        # it: a family error fetching an entry raises unnamed is this ls's
        # (`_attributed_entries`); what `on_entry` raises is the caller's own.
        with _scan_teardown(items, reusable_after_interrupt=self._reusable_after_interrupt):
            for info in _attributed_entries(items, "ls"):
                if cancel_token is not None and cancel_token.cancelled:
                    break
                on_entry(info)
                if cancel_token is not None and cancel_token.cancelled:
                    break
        _raise_if_cancelled(cancel_token, "ls")

    def _resolve_s3_target(self, target: Location, *, operation: str) -> S3Storage:
        """Resolve the single ``Location`` of an S3-only op to a validated ``S3Storage``.

        The shared seam of ``ls`` / ``rm`` / ``mb`` / ``rb`` / ``presign`` /
        ``website``. An ``S3Storage`` is used verbatim; a ``PathLike`` is expanded
        via ``os.fspath`` (honoring the ``Location`` os.PathLike[str] contract,
        like `resolve` does), and any other non-string - a non-S3 ``Storage``,
        which carries no ``__fspath__`` - falls through to the ``ValidationError``.
        Unlike `resolve`, these ops are lenient about the ``s3://`` scheme (a bare
        ``"bucket/key"`` or the ``"s3://"`` service root both work) and a non-s3
        string is never a local fallback; the built ``S3Storage`` carries this
        ``S3``'s `client`. Construction is permissive (non-raising), so the strict
        aws-cli checks (unsupported ARN forms, a key without a bucket) run via
        ``validate`` before the op uses the storage, followed by the S3 Express
        floor gate (`_validate_storage`).
        """
        if isinstance(target, S3Storage):
            storage = target
        else:
            if isinstance(target, os.PathLike):
                target = os.fspath(target)
            if not isinstance(target, str):
                raise ValidationError(
                    f"{operation} accepts an 's3://...' URI string or an S3Storage",
                    operation=operation,
                )
            storage = S3Storage(target, client=self.client())
        _validate_storage(storage, operation=operation)
        return storage

    # -- byte transfer ----------------------------------------------------

    def cp(
        self,
        src: Location,
        dest: Location,
        *,
        recursive: bool = False,
        filter: FileFilter | None = None,
        dryrun: bool = False,
        expected_size: int | None = None,
        on_progress: ProgressCallback | None = None,
        on_result: ResultCallback | None = None,
        cancel_token: CancelToken | None = None,
        transfer_config: TransferConfig | None = None,
        capture_response: bool = False,
        **options: Unpack[TransferOptions],
    ) -> None:
        """Copy bytes between ``src`` and ``dest`` with ``aws s3 cp`` semantics.

        The route follows the resolved pair: local->S3 upload, S3->local
        download, S3->S3 copy, or the open route pairing a custom ``Storage``
        with S3 (local->local is rejected, like aws-cli). Path
        shapes - what an existing-directory or trailing-slash destination
        means, which side's name wins, where ``--exclude`` / ``--include``
        patterns root - reproduce aws-cli's ``FileFormat`` rules
        (`boto3_s3.transferplan`); ``recursive`` is the aws-cli ``dir_op``.
        Bytes move through `boto3_s3.transfer.Transferrer`
        (s3transfer; multipart per ``transfer_config``, defaults matching
        aws-cli). Per-item parity behaviors carried over: local walk warnings
        (unreadable / special / broken-symlink files are skipped with a
        warning), GLACIER / DEEP_ARCHIVE sources skipped with a warning on
        download/copy unless forced (``ignore_glacier_warnings`` skips
        silently; a recursive listing carries no ``Restore``, so restored
        objects still skip there - aws-cli-faithful), parent-directory
        escapes blocked on download, the >48.8 TiB upload pre-warning,
        downloads stamping the source ``LastModified`` (a stamp failure is a
        warning, not an error), copy metadata/tags propagation per
        `copy_props` (including the library-only `annotation_copy_mode` for
        multipart `copy_props=ALL` staging), and a single S3 source resolved
        by HeadObject - a
        404 raises ``NotFoundError`` with aws-cli's rewritten ``Key "..." does
        not exist`` message. A keyless non-recursive S3 source matches
        nothing and transfers nothing (aws-cli lists and discards; the listing
        is issued here too, so its failure - NoSuchBucket, a denied
        ListBucket - stays observable instead of a silent rc 0).

        ``filter`` keeps an item in the operation (rm's contract): a
        ``FileFilter`` predicate over the item's ``FileInfo``, whose
        ``compare_key`` is stamped relative to aws-cli's base (the source
        directory or S3 ``Prefix``; a single source is shown aws-cli's own form,
        design/globsieve.md section 5) - a `GlobFilter` matches that key (a
        relative pattern) or the full path (an absolute one: a local entry's
        ``key``, an S3 entry's ``bucket/key``) while a richer
        predicate can read size / mtime / storage_class. On the stream route (an
        ``IOStorage`` on one side) there is no listing to prune, so ``filter``
        does not apply - aws-cli applies no filter to a stream either.
        How the local source is walked - whether symlinks are followed
        (``follow_symlinks``) and whether the recursive walk guards against symlink
        cycles (``detect_symlink_loops``, a library extension ``aws s3`` lacks),
        and whether every native entry becomes a filter candidate
        (``enumerate_all_entries``) - is
        configured on the ``LocalStorage`` itself (its constructor knobs), not on
        ``cp``: pass a configured ``LocalStorage`` as ``src`` to change it; a bare
        path string uses the defaults (follow symlinks, no cycle guard, normal
        transfer enumeration).
        The S3 listing page size is likewise the ``S3Storage``'s own ``page_size``.
        ``dryrun`` enumerates (listing and HeadObject still
        run) and reports ``OpOutcome.DRYRUN`` without transferring; warnings
        still apply. ``expected_size`` is the multipart sizing hint for a
        streaming upload (stdin -> S3): it is forwarded to the stream path
        (`_cp_stream` sets it as the upload item ``size_hint``, which the
        engine applies to a stream it cannot size - a non-seekable one - and
        ignores for a seekable stream it sizes itself) and is ignored on the
        non-stream routes, exactly like aws-cli's ``--expected-size`` (which
        applies to its non-seekable stdin alone). aws-cli documents the option as
        needed above 50 GB (with the default 8 MiB parts a hint-less stream
        runs out of parts above ~84 GB), but on the classic engine the hint
        decides the upload's shape at any size - a stream is sent as a single
        PutObject when the hint is below the multipart threshold. The CRT
        engine takes no size and ignores it, as it does for aws.

        Results stream to ``on_result`` from the engine's worker threads for
        submitted transfers; non-submitting records - dryrun, skips, notices,
        and some warnings - are emitted inline on the calling thread
        (design/opresult.md). Callbacks must be fast and
        non-raising. Failures aggregate into ``BatchError`` with
        the first failure as ``__cause__``, warnings alone do not raise (the
        CLI derives exit code 2 from its warned count). Failures *before*
        any item work - an unreadable destination, the missing-source check
        - raise directly: a missing local source raises ``NotFoundError``
        (aws-cli's pre-pipeline rc 255 shape - no ``ClientError`` cause, so the
        CLI maps it to the general rc).
        """
        _validate_transfer_options(options, operation="cp")
        src_storage = self.resolve(src)
        dest_storage = self.resolve(dest)
        # The strict location checks (construction was permissive; a stream
        # side validates as a no-op).
        _validate_storage(src_storage, operation="cp")
        _validate_storage(dest_storage, operation="cp")
        transfer_config = self._run_transfer_config(
            transfer_config, src_storage, dest_storage, operation="cp"
        )
        options = self._upload_options(options, src_storage, dest_storage, transfer_config)
        if isinstance(src_storage, IOStorage) or isinstance(dest_storage, IOStorage):
            self._cp_stream(
                src_storage,
                dest_storage,
                recursive=recursive,
                dryrun=dryrun,
                expected_size=expected_size,
                on_progress=on_progress,
                on_result=on_result,
                cancel_token=cancel_token,
                transfer_config=transfer_config,
                capture_response=capture_response,
                options=options,
            )
            return
        self._run_transfer(
            src_storage,
            dest_storage,
            operation="cp",
            is_move=False,
            recursive=recursive,
            item_filter=filter,
            dryrun=dryrun,
            on_progress=on_progress,
            on_result=on_result,
            cancel_token=cancel_token,
            transfer_config=transfer_config,
            capture_response=capture_response,
            options=options,
        )

    def _run_transfer(
        self,
        src_storage: Storage,
        dest_storage: Storage,
        *,
        operation: str,
        is_move: bool,
        recursive: bool,
        item_filter: FileFilter | None,
        dryrun: bool,
        on_progress: ProgressCallback | None,
        on_result: ResultCallback | None,
        cancel_token: CancelToken | None,
        transfer_config: TransferConfig | None,
        capture_response: bool = False,
        options: TransferOptions,
    ) -> None:
        """The shared cp/mv pipeline for parsed, validated, non-stream locations.

        Route classification, the pre-batch checks, enumeration, the gates,
        and the submit loop - identical for both operations; ``mv`` differs
        only by what it validated beforehand and by ``is_move`` (the engine's
        delete-source + MOVE reporting). Both sides passed the strict
        location checks in the caller, ahead of its own gates.

        The built-in routes assert the concrete ``LocalStorage`` / ``S3Storage``
        pair, because the engine reaches into ``S3Storage``'s client/bucket and
        ``LocalStorage``'s path directly (s3transfer). A custom ``Storage`` (any
        other ``scheme``) pairs with S3 through the ``open`` route (``opens3`` /
        ``s3open``): its bytes move via ``Storage.open`` while the S3 side rides
        s3transfer. The custom side is capability-checked up front - it must
        implement the contract methods the route uses - so a missing method
        surfaces as a clear ``ValidationError`` instead of failing deep in the
        engine.
        """
        # A pre-cancelled token acts before this method's side effects: the
        # destination pre-create below, the case-gate's destination walk, and
        # any lazily-deferred client build (a caller-made S3Storage without a
        # client; resolve() above already built clients for string arguments).
        # ls / rm poll right after resolution the same way.
        _raise_if_cancelled(cancel_token, operation)
        plan = transferplan.plan_transfer(
            src_storage, dest_storage, recursive=recursive, operation=operation
        )

        transfer_type, client_provider, source_provider, dest_bucket = _classify_transfer_route(
            plan, operation=operation
        )
        client = client_provider.get_client()
        source_client = source_provider.get_client() if source_provider is not None else None
        # The S3 listing source (s3local / s3s3 / s3open); None on the
        # local/custom-source upload routes.
        src_s3 = src_storage if isinstance(src_storage, S3Storage) else None

        producers.require_open_capabilities(
            plan, recursive=recursive, is_move=is_move, operation=operation
        )
        transferrer = Transferrer(
            transfer_type,
            client,
            source_client=source_client,
            # The run's two resolved sides, carried onto each result.
            src_storage=src_storage,
            dest_storage=dest_storage,
            transfer_config=transfer_config,
            options=options,
            operation=operation,
            is_move=is_move,
            on_progress=on_progress,
            on_result=on_result,
            cancel_token=cancel_token,
            capture_response=capture_response,
            crt=self._crt,
        )
        # The case-conflict gate's mode is a set-up check on every route; the
        # gate itself is built below, once the destination directory exists.
        producers.case_conflict_mode(options, operation=operation)
        with transferrer:
            if not dryrun:
                # Build the manager (registers client-event handlers) before the
                # producers start their scan prefetch worker on this same client
                # (Transferrer.prepare). A CRT engine that cannot be built
                # refuses the run here.
                transferrer.prepare()
            # The destination directory, created only once every set-up check
            # has passed, so a refused run changes nothing (aws-cli creates it
            # in its path validation, before the rest). aws-cli's
            # _validate_path_args only creates the dest dir when it does not
            # already exist, testing and creating the path as typed -
            # ensure_directory does both on the construction-time anchor
            # (consistent with the plan if the process chdir'd since). An
            # existing *file* written without a trailing separator counts as
            # existing: it skips makedirs and fails per item like aws-cli (rc 1)
            # instead of crashing up front, and an empty listing transfers
            # nothing and exits 0. (sync's guard is the same call, not gated on
            # recursive.)
            if recursive and isinstance(dest_storage, LocalStorage):
                try:
                    dest_storage.ensure_directory()
                except (OSError, ValueError) as exc:
                    raise translate_os_error(exc, operation=operation, key=None) from exc
            # After the Transferrer: the gate's destination membership scan
            # warns into the shared rollup (aws-cli's reverse enumeration shares
            # the result queue) and applies the run's filter, like any side's
            # walk.
            case_gate = producers.cp_case_gate(
                plan,
                recursive=recursive,
                options=options,
                transferrer=transferrer,
                item_filter=item_filter,
                operation=operation,
                reusable_after_interrupt=self._reusable_after_interrupt,
            )
            if plan.paths_type == "opens3":
                items = producers.open_upload_items(
                    plan,
                    dest_bucket=dest_bucket,
                    transferrer=transferrer,
                    item_filter=item_filter,
                    operation=operation,
                    dryrun=dryrun,
                    reusable_after_interrupt=self._reusable_after_interrupt,
                )
            elif plan.paths_type == "s3open":
                assert src_s3 is not None
                items = producers.open_download_items(
                    plan,
                    src_s3,
                    transferrer=transferrer,
                    item_filter=item_filter,
                    options=options,
                    operation=operation,
                    dryrun=dryrun,
                    reusable_after_interrupt=self._reusable_after_interrupt,
                )
            elif src_s3 is None:
                items = producers.upload_items(
                    plan,
                    dest_bucket=dest_bucket,
                    transferrer=transferrer,
                    item_filter=item_filter,
                    operation=operation,
                    reusable_after_interrupt=self._reusable_after_interrupt,
                )
            else:
                items = producers.s3_source_items(
                    plan,
                    src_s3,
                    transfer_type=transfer_type,
                    dest_bucket=dest_bucket,
                    transferrer=transferrer,
                    item_filter=item_filter,
                    options=options,
                    case_gate=case_gate,
                    operation=operation,
                    reusable_after_interrupt=self._reusable_after_interrupt,
                )
            # Close the producer (joining its scan prefetch worker) before
            # Transferrer.__exit__ shuts down and unregisters the capture
            # handlers - the teardown mirror of prepare()'s
            # register-before-enumeration ordering, so no listing traffic
            # emits on the client during the deregistration.
            with _producer_teardown(items, reusable_after_interrupt=self._reusable_after_interrupt):
                # Check cancellation *before* pulling the next item, not after:
                # pulling only while the run is live keeps a cancelled run from
                # enumerating and materializing items it will never submit - the
                # listing stops promptly instead of paging on. (The open routes'
                # deferred handles never open for an unsubmitted item, so no
                # resource is at stake; this is about not doing pointless work.)
                _raise_if_cancelled(cancel_token, operation)
                # The pull runs the enumeration - a listing page, the source
                # walk - inside a storage that cannot know the operation: what
                # it raises unnamed is this run's.
                for item in _attributed_entries(items, operation):
                    if dryrun:
                        transferrer.dryrun(item)
                    else:
                        transferrer.submit(item)
                    _raise_if_cancelled(cancel_token, operation)
        _raise_if_cancelled(cancel_token, operation)
        if transferrer.failed:
            raise BatchError(
                f"{transferrer.failed} of "
                f"{transferrer.failed + transferrer.succeeded} transfers failed",
                succeeded=transferrer.succeeded,
                failed=transferrer.failed,
                warned=transferrer.warned,
                skipped=transferrer.skipped,
                operation=operation,
            ) from transferrer.first_error

    def _cp_stream(
        self,
        src_storage: Storage,
        dest_storage: Storage,
        *,
        recursive: bool,
        dryrun: bool,
        expected_size: int | None,
        on_progress: ProgressCallback | None,
        on_result: ResultCallback | None,
        cancel_token: CancelToken | None,
        transfer_config: TransferConfig | None,
        capture_response: bool = False,
        options: TransferOptions,
    ) -> None:
        """One streaming transfer: an ``IOStorage`` on exactly one side.

        The other side must be S3 (a non-S3 peer is the "stream on one side"
        error); its key is taken verbatim (the CLI owns aws-cli's ``-``-basename
        naming quirk). The stream's fileobj comes from ``IOStorage.open`` and is
        handed straight to ``s3transfer``. Uploads carry ``expected_size`` as the
        multipart sizing hint of a stream the engine cannot size (without it the
        engine buffers up to the threshold and decides; a seekable stream is
        sized by the engine and the hint is ignored); downloads provide neither
        size nor etag, so s3transfer
        probes the object with a HeadObject - the aws-cli stream wire shape. Streams
        are single items: the glacier / parent-ref gates do not apply; displays
        render as ``-``. A ``cancel_token`` already cancelled raises
        ``CancelledError`` before the fileobj is opened or submitted, matching the
        non-stream route's pre-submit poll (a single item, so one check suffices).
        """
        src_is_stream = isinstance(src_storage, IOStorage)
        dest_is_stream = isinstance(dest_storage, IOStorage)
        if recursive:
            raise ValidationError(
                "Streaming currently is only compatible with non-recursive cp commands",
                operation="cp",
            )
        if src_is_stream and dest_is_stream:
            raise ValidationError(
                "cp supports a stream on one side only (the other must be s3://)",
                operation="cp",
            )
        if dest_is_stream and options.get("no_overwrite"):
            # A streaming download has no existing destination to guard, so
            # no_overwrite is meaningless here (aws-cli rejects it too); fail
            # loud rather than silently ignore. Uploads keep IfNoneMatch.
            raise ValidationError(
                "no_overwrite is not supported for streaming downloads",
                operation="cp",
            )
        # Poll once before opening the fileobj / submitting, like the
        # non-stream loop does before pulling each item - a pre-cancelled
        # run transfers nothing and leaves a side-effecting stream untouched.
        _raise_if_cancelled(cancel_token, "cp")
        # A stream is a single item with no destination tree, so no case-conflict
        # gate is built here; the value is still read (and refused if bad) so
        # every cp validates the option, whatever its paths - before the peer's
        # client, the fileobj open, and the engine.
        producers.case_conflict_mode(options, operation="cp")
        if src_is_stream:
            storage = self._stream_s3_peer(dest_storage)
            transfer_type = TransferType.UPLOAD
            # dryrun reports the item without submitting it, so skip the open -
            # like the open routes (open_upload_item / open_download_item),
            # keeping a side-effecting stream untouched on a dry run. A live
            # run opens eagerly here, unlike those routes' deferred handles:
            # a stream storage wraps an already-open in-hand stream (no
            # resource is acquired), it is a single item (nothing queues
            # up), and StdioStorage's missing-stdio ValidationError keeps
            # its documented in-pipeline slot - attributed to cp here, since
            # the storage cannot know which operation opened it.
            with _attributed_to("cp"):
                src_fileobj = None if dryrun else src_storage.open(storage.key, "rb")
            item = TransferItem(
                compare_key=storage.key,
                size_hint=expected_size,
                src_fileobj=src_fileobj,
                dest_bucket=storage.bucket,
                dest_key=storage.key,
                src_display="-",
                dest_display=storage.as_text(),
            )
        else:
            storage = self._stream_s3_peer(src_storage)
            transfer_type = TransferType.DOWNLOAD
            # See the upload branch: a dry run never opens the stream.
            with _attributed_to("cp"):
                dest_fileobj = None if dryrun else dest_storage.open(storage.key, "wb")
            item = TransferItem(
                compare_key=storage.key,
                src_bucket=storage.bucket,
                src_key=storage.key,
                dest_fileobj=dest_fileobj,
                # `aws` prints the source as its argument normalization left it:
                # a keyless one is the bucket root, "s3://b/" (or "s3:///").
                src_display=storage.normalized_uri(),
                dest_display="-",
            )
        transferrer = Transferrer(
            transfer_type,
            storage.get_client(),
            src_storage=src_storage,
            dest_storage=dest_storage,
            transfer_config=transfer_config,
            options=options,
            operation="cp",
            on_progress=on_progress,
            on_result=on_result,
            cancel_token=cancel_token,
            capture_response=capture_response,
            crt=self._crt,
        )
        with transferrer:
            if dryrun:
                transferrer.dryrun(item)
            else:
                transferrer.submit(item)
        _raise_if_cancelled(cancel_token, "cp")
        if transferrer.failed:
            raise BatchError(
                "1 of 1 transfers failed",
                succeeded=transferrer.succeeded,
                failed=transferrer.failed,
                warned=transferrer.warned,
                skipped=transferrer.skipped,
                operation="cp",
            ) from transferrer.first_error

    @staticmethod
    def _stream_s3_peer(peer: Storage) -> S3Storage:
        """The S3 side facing a stream in ``_cp_stream``.

        ``cp`` already resolved and validated both sides, so the stream's peer
        is a ``Storage`` instance: an ``S3Storage`` is the only well-formed one
        (a local or other non-S3 peer is the "stream on one side" error, not
        the generic `_resolve_s3_target` message, which reads as if ``cp``
        never takes a local path).
        """
        if not isinstance(peer, S3Storage):
            raise ValidationError(
                "cp supports a stream on one side only (the other must be s3://)",
                operation="cp",
            )
        return peer

    def mv(
        self,
        src: Location,
        dest: Location,
        *,
        recursive: bool = False,
        filter: FileFilter | None = None,
        dryrun: bool = False,
        on_progress: ProgressCallback | None = None,
        on_result: ResultCallback | None = None,
        cancel_token: CancelToken | None = None,
        transfer_config: TransferConfig | None = None,
        capture_response: bool = False,
        **options: Unpack[TransferOptions],
    ) -> None:
        """Move bytes with ``aws s3 mv`` semantics: ``cp``, then delete the source.

        Everything `cp` documents - routes, path shapes, filters,
        gates, warnings, the ``BatchError`` aggregation - applies unchanged;
        the differences are mv's. Every result reports ``TransferType.MOVE``, and
        each item's source is deleted right after its transfer succeeds
        (``Storage.delete`` for uploads - a local file or a custom backend
        object; one DeleteObject per object otherwise, ``request_payer``
        forwarded). A failed, skipped, or dry-run item
        keeps its source; a deletion failure turns that item into the
        failure ``aws`` prints as ``move failed`` (the bytes already arrived).
        Filters prune both the transfer and the deletion. A stream
        (``IOStorage``) can be the destination of a single-object move -
        the bytes land on the stream, then the S3 source is deleted - but
        not a recursive one (a stream is a single endpoint) and never the
        source (a move deletes its source, which a stream cannot be); both
        raise ``ValidationError``. The CLI rejects ``-`` for mv on either
        side outright (aws-cli parity). Emptied
        local source directories are left behind like aws.

        Moving an object onto itself (same URI, or a ``/``-terminated
        destination plus the source's basename - checked for ``--recursive``
        too, aws-cli's faithful false positive) raises
        ``ValidationError`` before anything runs. That
        guard is textual only: paths through access point ARNs or aliases
        can still land on the same underlying bucket. Resolve them first
        with `boto3_s3.pathresolver.S3PathResolver` (the
        ``--validate-same-s3-paths`` machinery; bring your own s3control /
        sts clients) when that risk applies.
        """
        _validate_transfer_options(options, operation="mv")
        src_storage = self.resolve(src)
        dest_storage = self.resolve(dest)
        # The strict location checks (see cp).
        _validate_storage(src_storage, operation="mv")
        _validate_storage(dest_storage, operation="mv")
        transfer_config = self._run_transfer_config(
            transfer_config, src_storage, dest_storage, operation="mv"
        )
        options = self._upload_options(options, src_storage, dest_storage, transfer_config)
        if isinstance(src_storage, IOStorage):
            # A move deletes its source, which a stream cannot be (no delete).
            # The destination side is different: a single-object move onto a
            # stream is well-formed (bytes land on the stream, then the S3
            # source is deleted) and rides the s3open route below.
            raise ValidationError(
                "mv does not support a stream source: a move deletes its source, "
                "which a stream cannot be",
                operation="mv",
            )
        if isinstance(dest_storage, IOStorage) and recursive:
            # A stream is a single endpoint: a recursive move would concatenate
            # every object into it while deleting the sources.
            raise ValidationError(
                "mv supports a stream destination only for a single object (non-recursive)",
                operation="mv",
            )
        if isinstance(dest_storage, IOStorage) and options.get("no_overwrite"):
            # cp's stream gate, ported: a streaming download has no existing
            # destination to guard, so no_overwrite is meaningless - fail loud
            # rather than silently ignore it and delete the source anyway.
            raise ValidationError(
                "no_overwrite is not supported for streaming downloads",
                operation="mv",
            )
        if isinstance(src_storage, S3Storage) and isinstance(dest_storage, S3Storage):
            if src_storage.same_path_as(dest_storage):
                # aws-cli words the error with the keyless-normalized URIs
                # (`mv s3://b/k s3://b` reports `s3://b/`).
                raise ValidationError(
                    f"Cannot mv a file onto itself: {src_storage.normalized_uri()} "
                    f"- {dest_storage.normalized_uri()}",
                    operation="mv",
                )
        self._run_transfer(
            src_storage,
            dest_storage,
            operation="mv",
            is_move=True,
            recursive=recursive,
            item_filter=filter,
            dryrun=dryrun,
            on_progress=on_progress,
            on_result=on_result,
            cancel_token=cancel_token,
            transfer_config=transfer_config,
            capture_response=capture_response,
            options=options,
        )

    def sync(
        self,
        src: Location,
        dest: Location,
        *,
        filter: FileFilter | None = None,
        create_filter: bool | FileFilter | ParallelFilter[FileInfo] = True,
        update_filter: bool | PairFilter | ParallelFilter[SyncPair] | None = None,
        delete_filter: bool | FileFilter | ParallelFilter[FileInfo] = False,
        pair_filter: MergedPairFilter | None = None,
        dryrun: bool = False,
        on_progress: ProgressCallback | None = None,
        on_result: ResultCallback | None = None,
        cancel_token: CancelToken | None = None,
        transfer_config: TransferConfig | None = None,
        capture_response: bool = False,
        **options: Unpack[TransferOptions],
    ) -> None:
        """Recursively synchronize ``src`` into ``dest`` (``aws s3 sync``).

        Always recursive (no single-file sync, aws-cli parity) over the three
        built-in transfer routes plus the open route (a custom ``Storage``
        paired with S3); a local->local pair raises ``ValidationError``. The
        pipeline has two layers:

        - **Visibility** - both listings are pruned independently *before*
          the sides meet by the single ``filter``, a
          `FileFilter` applied to each side's `compare_key` (directory-relative
          for local entries, ``Prefix``-relative for S3 entries) - so it prunes
          the source and destination **symmetrically** (matching aws-cli, which evaluates
          ``--exclude`` / ``--include`` against both sides). Folder markers
          (zero-byte ``/``-terminated objects) never surface on either side -
          sync neither transfers nor deletes them. Because pruning is
          symmetric, an excluded key is invisible on *both* sides and is thus
          neither transferred nor deleted (aws-cli's "files excluded by filters
          are excluded from deletion"). The exception is a side whose
          relative path the filter sees with a leading ``/`` (a local side
          rooted at a filesystem root, a Windows S3 base ending in a backslash,
          as on aws-cli): a name-led relative pattern then hides a key on the
          other side only, which can leave it to ``delete_filter``. Per-lane narrowing
          belongs in the pair layer below (``create_filter`` / ``update_filter``
          / ``delete_filter``).
        - **Pair decisions** - the surviving streams are merge-joined by
          compare key (`Comparator`) and each resulting pair lands in exactly
          one of three lanes by its type (`MergedPair` - the shape says which
          sides hold the key), each a filter deciding whether to act:
          **create** a new entry, **overwrite** one on both sides, or **delete**
          an orphan. ``create_filter`` and ``delete_filter`` are the two
          membership knobs (create / delete) - duals of each other;
          ``update_filter`` is the overwrite judgment for the intersection. The
          aws-cli equivalent is the default of all three:

          - ``create_filter`` - the **new** (`SrcOnlyPair`) entries: ``True``
            (default) copies every one, ``False`` none, a
            `FileFilter` only those it keeps (matched
            against the source ``FileInfo`` / compare key, the same shape as
            ``rm``'s ``filter``). aws-cli hard-codes "always create"
            (``file_not_at_dest``), so this knob has no aws-cli counterpart.
          - ``update_filter`` - the **update** (`SyncPair`) pairs: ``None``
            (default) is the aws-cli size + last-modified judgment, equivalently
            ``AwsCliComparison()`` - tune it with
            ``AwsCliComparison(size_only=...)`` / ``(exact_timestamps=...)``;
            ``True`` re-copies every one, ``False`` none (additive-only:
            existing destinations are left as-is); any
            `PairFilter` is a custom strategy - the
            content building blocks ``EtagComparison`` / ``ChecksumComparison``
            (submodule imports) are drop-in replacements that compare by
            content. A `SyncPair` always carries both sides, so a custom
            strategy reads ``pair.src`` / ``pair.dest`` directly.
          - ``delete_filter`` - the **orphan** (`DestOnlyPair`) entries:
            ``False`` (default) deletes nothing, ``True`` deletes every one
            (aws-cli's ``--delete``), a `FileFilter` only those
            it keeps (same shape as ``rm``'s ``filter``).

          Any lane's filter may be wrapped in
          `ParallelFilter` to run its per-entry
          decision on a caller-supplied thread pool (for a content
          ``update_filter=`` strategy, or a ``create`` / ``delete`` filter that
          reads bytes / tags / attributes) - identical results, only faster (the
          wrapped filter must be thread-safe). Parallelizing ``create_filter``
          makes the ``--case-conflict`` "first key wins" order non-deterministic.

          ``pair_filter`` **replaces** all three lanes with one
          `MergedPairFilter`: every merged pair - `SrcOnlyPair`, `SyncPair` and
          `DestOnlyPair` alike - is passed to it serially, on this calling
          thread, in the merge's order (ascending compare-key order when both
          listings arrive in key order), and ``True`` means "take this
          pair's default action" (copy a new entry, copy an update, delete an
          orphan). It is for an application that needs one view of everything
          the run decides - an audit journal, a confirmation flow, statistics,
          or any decision sharing state across lanes - instead of wiring one
          object's methods into three filters. Returning ``False`` for every
          `DestOnlyPair` is the supported observe-only mode: the delete lane is
          live (so a walked local destination is listed without read-ahead, and
          an open-route destination must declare ``DELETE``), and nothing is
          removed. Passing it together with a non-default ``create_filter`` /
          ``update_filter`` / ``delete_filter``, with ``no_overwrite=True``
          (which would drop the update lane and hide those pairs), or as a
          `ParallelFilter` (the serial order is the contract) raises
          ``ValidationError``. ``filter`` still composes ahead of it: a key
          pruned by visibility never reaches ``pair_filter``. A lane filter
          that is none of its shapes - ``None`` on ``create_filter`` /
          ``delete_filter``, an integer - raises ``ValidationError`` before
          anything is resolved rather than failing as a ``TypeError`` once
          the run first calls it.

          ``no_overwrite`` is an orthogonal write-guard on the update lane: an
          existing destination is never overwritten (new entries still copy),
          and sync keeps it decision-only - no ``IfNoneMatch`` on the wire.

        Transfers run on the engine with cp's gates (glacier, the
        parent-directory guard, the ``--case-conflict`` gate for downloads -
        applied only to `SrcOnlyPair`s, the aws-cli not-at-dest slot).
        Deletions are dispatched as they stream: batched ``DeleteObjects`` for
        an S3 destination, with XML-incompatible keys falling back to
        ``DeleteObject`` (the ``rm`` machinery; ``request_payer`` forwarded),
        a synchronous ``Storage.delete`` for a local one
        (``LocalStorage.delete``, an ``os.remove``) or a custom (open-route)
        one (through the backend's own ``delete``). ``dryrun``
        reports every would-be transfer and deletion without any mutating
        API call (both sides' listings still run).
        Local listing warnings (unreadable / vanished / special files,
        invalid timestamps) surface from **both** sides as WARNED records,
        exactly like aws-cli walking both trees.

        A missing local source directory raises (aws-cli's ``The user-provided
        path ... does not exist.``); a local destination directory is
        created up front even when nothing transfers. An S3 Express
        directory bucket or one of its access points on either side is
        then rejected (aws-cli's ``Cannot use sync command with a directory
        bucket.``): its unordered listings would break the sorted
        merge-join. Item failures -
        transfer and delete alike - aggregate into one ``BatchError`` with
        rollup counts (a failure sample as ``__cause__``: the transfer
        rollup's first, else the delete rollup's). `cancel_token` raises
        `CancelledError` after shutdown: graceful mode stops new pair actions
        and drains accepted transfers/deletes; immediate mode additionally
        requests best-effort future cancellation.
        """
        _validate_transfer_options(options, operation="sync")
        # Shape checks over the arguments alone: refuse before any resolution
        # or side effect (the destination pre-create below).
        reject_lane_filter_shapes(
            create_filter=create_filter, update_filter=update_filter, delete_filter=delete_filter
        )
        if pair_filter is not None:
            reject_pair_filter_conflicts(
                pair_filter,
                create_filter=create_filter,
                update_filter=update_filter,
                delete_filter=delete_filter,
                no_overwrite=bool(options.get("no_overwrite", False)),
            )
        src_storage = self.resolve(src)
        dest_storage = self.resolve(dest)
        _validate_storage(src_storage, operation="sync")
        _validate_storage(dest_storage, operation="sync")
        transfer_config = self._run_transfer_config(
            transfer_config, src_storage, dest_storage, operation="sync"
        )
        options = self._upload_options(options, src_storage, dest_storage, transfer_config)
        # A pre-cancelled token acts before this method's side effects (the
        # destination pre-create below, any lazily-deferred client build - a
        # caller-made S3Storage without a client; resolve() above already
        # built clients for string arguments), like cp/mv/ls/rm.
        _raise_if_cancelled(cancel_token, "sync")
        plan = transferplan.plan_transfer(
            src_storage, dest_storage, recursive=True, operation="sync"
        )

        transfer_type, client_provider, source_provider, dest_bucket = _classify_transfer_route(
            plan, operation="sync"
        )
        # An S3 Express directory bucket lists in unspecified order
        # (`ListObjectsV2` drops the lexicographic guarantee), which would feed
        # the sorted merge-join unsorted input - `delete_filter` could then
        # remove from the destination keys the source has too, as aws-cli's
        # merge would for any unsorted listing. aws-cli rejects
        # sync outright on either side
        # (`_validate_not_s3_express_bucket_for_sync`), after creating the
        # local destination directory, which is created here only once the run
        # is past every such check (below); so does the CLI layer, making
        # this the backstop for direct library callers. An access point of a
        # directory bucket (`--xa-s3`) lists the same keys in the same
        # unordered way (measured on real S3: 18 of 20 identical objects
        # deleted by one `delete_filter` run), so it is rejected too, which
        # aws-cli's `--x-s3`-only test does not do.
        for side_storage in (src_storage, dest_storage):
            if isinstance(side_storage, S3Storage) and side_storage.bucket.endswith(
                ("--x-s3", "--xa-s3")
            ):
                raise ValidationError(
                    "Cannot use sync command with a directory bucket.", operation="sync"
                )
        client = client_provider.get_client()
        source_client = source_provider.get_client() if source_provider is not None else None
        # A custom side must support sorted enumeration (the merge-join) plus the
        # I/O the route uses; reject up front before any listing (gate below).
        # A pair_filter judges the orphans too, so it asks for the delete lane's
        # capabilities even though it may decide to delete nothing. The lane is
        # on for anything but the literal False (below), so that is the test
        # here too - not truthiness, which a callable is free to define.
        producers.require_open_sync_capabilities(
            plan, delete=delete_filter is not False or pair_filter is not None, operation="sync"
        )

        # no_overwrite is an orthogonal write-guard (an option, so callers can
        # write ``sync(no_overwrite=True)``): strip it from the engine options
        # and apply it in the loop, keeping sync decision-only (no IfNoneMatch).
        no_overwrite = options.pop("no_overwrite", False)
        create_decision, update_decision, delete_decision = resolve_lanes(
            create_filter=create_filter,
            update_filter=update_filter,
            delete_filter=delete_filter,
            pair_filter=pair_filter,
        )
        case_gate = producers.sync_case_gate(transfer_type, dest_storage, options=options)

        transferrer = Transferrer(
            transfer_type,
            client,
            source_client=source_client,
            src_storage=src_storage,
            dest_storage=dest_storage,
            transfer_config=transfer_config,
            options=options,
            operation="sync",
            on_progress=on_progress,
            on_result=on_result,
            cancel_token=cancel_token,
            capture_response=capture_response,
            crt=self._crt,
        )
        deletes = SyncDeletes(
            dest_storage,
            request_payer=options.get("request_payer"),
            dryrun=dryrun,
            on_result=on_result,
            cancel_token=cancel_token,
            capture_response=capture_response,
            # aws-cli runs an S3-to-S3 sync on its classic transfer client
            # whatever the preference (`_compute_transfer_client_type`), so
            # only an upload's deletes can ride its CRT client.
            transfer_config=(
                transferconfig.TransferConfig(preferred_transfer_client="classic")
                if transfer_type is TransferType.COPY
                else transfer_config
            ),
            crt=self._crt,
        )
        with ExitStack() as stack:
            stack.enter_context(transferrer)
            if not dryrun:
                # Build the manager before either side's scan prefetch worker
                # starts on the manager's client (Transferrer.prepare).
                transferrer.prepare()
            deletes.open(stack)
            # The destination directory, created only once every set-up check
            # has passed, so a refused run changes nothing; aws-cli creates it
            # during its path validation, before the rest. Still before any
            # listing, so even an empty sync leaves it behind, as on aws-cli.
            # The bare exists() test (not exist_ok=True) is deliberate: a
            # destination that exists as a *file* - written without a trailing
            # separator - passes here and fails per item instead ([Errno 20],
            # rc 1). (cp's guard is the same call, gated on recursive.)
            if isinstance(dest_storage, LocalStorage):
                try:
                    dest_storage.ensure_directory()
                except (OSError, ValueError) as exc:
                    raise translate_os_error(exc, operation="sync", key=None) from exc
            src_entries = producers.sync_entries(
                src_storage,
                root=plan.src_root,
                item_filter=filter,
                transferrer=transferrer,
                options=options,
                reusable_after_interrupt=self._reusable_after_interrupt,
            )
            dest_entries = producers.sync_entries(
                dest_storage,
                root=plan.dest_root,
                item_filter=filter,
                transferrer=transferrer,
                options=options,
                reusable_after_interrupt=self._reusable_after_interrupt,
                # The delete lane removes this side's orphans while this same
                # listing is still running, so a walked destination must not be
                # read ahead of it (see sync_entries).
                deletes_orphans=delete_decision is not None,
            )
            # Close both sides' producers before the stack unwinds into
            # Transferrer.__exit__'s capture deregistration - the teardown
            # mirror of prepare()'s register-before-enumeration ordering.
            stack.enter_context(
                _producer_teardown(
                    dest_entries,
                    src_entries,
                    reusable_after_interrupt=self._reusable_after_interrupt,
                )
            )

            def submit_copy(pair: SrcOnlyPair | SyncPair) -> None:
                item = producers.sync_transfer_item(
                    plan,
                    pair,
                    transfer_type=transfer_type,
                    dest_bucket=dest_bucket,
                    transferrer=transferrer,
                    options=options,
                    case_gate=case_gate,
                    dryrun=dryrun,
                )
                if item is None:
                    return
                if dryrun:
                    transferrer.dryrun(item)
                else:
                    transferrer.submit(item)

            # no_overwrite rules out every update up front (an existing
            # destination is never overwritten), so the update lane is dropped.
            # A family error a decision raises unnamed - a content strategy's
            # ``Storage.open`` of one side, which cannot know the operation -
            # aborts the run attributed to sync, like the storage checks above.
            with _attributed_to("sync"):
                run_sync_pairs(
                    Comparator(transfer_type).compare(src_entries, dest_entries),
                    create_lane=Lane(create_decision.decide, submit_copy, create_decision.executor),
                    update_lane=(
                        None
                        if no_overwrite
                        else Lane(update_decision.decide, submit_copy, update_decision.executor)
                    ),
                    delete_lane=(
                        None
                        if delete_decision is None
                        else Lane(delete_decision.decide, deletes.submit, delete_decision.executor)
                    ),
                    cancel_token=cancel_token,
                )

        _raise_if_cancelled(cancel_token, "sync")
        failed = transferrer.failed + deletes.failed
        if failed:
            succeeded = transferrer.succeeded + deletes.succeeded
            raise BatchError(
                f"{failed} of {failed + succeeded} operations failed",
                succeeded=succeeded,
                failed=failed,
                warned=transferrer.warned,
                skipped=transferrer.skipped,
                operation="sync",
            ) from (transferrer.first_error or deletes.first_error)

    def rm(
        self,
        target: Location,
        *,
        recursive: bool = False,
        filter: FileFilter | None = None,
        dryrun: bool = False,
        request_payer: str | None = None,
        on_result: ResultCallback | None = None,
        cancel_token: CancelToken | None = None,
        capture_response: bool = False,
        transfer_config: TransferConfig | None = None,
    ) -> None:
        """Delete objects under ``target`` with ``aws s3 rm`` semantics.

        Three target shapes (aws-cli parity, ``FileGenerator.list_objects``):

        - **key, non-recursive**: one *blind* ``DeleteObject`` of exactly that
          key - no listing, no HeadObject, and deleting a nonexistent key
          succeeds. A trailing-``/`` key deletes that "folder marker" object.
        - **recursive**: every object under the ``/``-normalized prefix
          (``"data"`` lists under ``"data/"``, so ``data-sibling.txt`` is not
          touched), folder markers included, deleted in batched
          ``DeleteObjects`` calls via `S3Deleter`; XML-incompatible keys fall
          back to aws-cli's per-key ``DeleteObject`` route (design/deleter.md).
        - **bucket root, non-recursive**: lists the whole bucket but deletes
          only zero-byte ``/``-terminated "folder marker" objects (any depth)
          - aws-cli's manual-folder sweep, not a full wipe.

        ``filter`` keeps an item in the deletion when it is *included*: a
        ``Callable[[FileInfo], bool]`` over the entry's ``FileInfo``. Its
        ``compare_key`` is stamped to the key relative to the prefix returned by
        `rm_filter_root` before the call (with the Windows exception it
        describes), so a `GlobFilter` matches that key as aws-cli matches it
        (the CLI maps ``--exclude`` / ``--include`` here) while a
        richer predicate can read ``size`` / ``mtime`` / ``storage_class`` (on
        the blind single-key path ``size`` / ``mtime`` / ``storage_class`` are
        all ``None`` - only ``key``, ``compare_key`` and ``storage`` are set).
        On the enumerating paths it is evaluated page by page on
        the scan prefetch worker (via ``ScanOptions.filter``), overlapped with
        the listing I/O - keep the filter thread-safe and fast, like
        ``on_result``. ``dryrun`` enumerates (the recursive listing still
        runs) and reports candidates as ``OpOutcome.DRYRUN`` without any
        delete calls.

        Per-item completion is streamed to ``on_result`` (from the deleter's
        worker thread on the batched path - keep the callback fast and
        non-raising). Item failures are aggregated: ``BatchError`` with rollup
        counts (first failure as ``__cause__``) once at the end, exit-code
        model design/exceptions.md section 4. Failures *before* any item work - e.g.
        the listing rejecting the bucket - raise their category error
        directly. `cancel_token` may be cancelled from `on_result`: graceful
        mode discards the unsent buffer and drains the in-flight delete batch;
        immediate mode also cancels an unstarted batch future. Both raise
        `CancelledError` after worker cleanup.

        ``transfer_config`` (this instance's when ``None``) moves no bytes here:
        its engine choice is the client the deletes ride, as ``aws s3 rm``
        carries them on its transfer manager - botocore, or the CRT client when
        it selects the CRT engine (`crt_delete_sender`, with this instance's
        CRT postures). That client's retry policy and error text are the
        deletes'. A delete that dies without any response fails its key on
        either client, where aws-cli's classic client loses it with no line.
        """
        text = os.fspath(target) if isinstance(target, (str, os.PathLike)) else None
        if isinstance(text, str):
            # A string reads as `aws s3 rm` reads its argument - the same
            # keyless normalization `resolve` applies for cp / mv / sync - with
            # the scheme this operation lets a caller omit put back first, so
            # both spellings name the same location. Anything else is left to
            # `_resolve_s3_target`'s type check.
            if not text.startswith("s3://"):
                text = f"s3://{text}"
            target = S3Storage.normalize_s3_uri(text)
        storage = self._resolve_s3_target(target, operation="rm")
        with _attributed_to("rm"):
            config = self.resolve_transfer_config(transfer_config)
            self._require_crt_region(config)
        _raise_if_cancelled(cancel_token, "rm")
        if not storage.bucket:
            # rm has no bucket-listing mode (scan is object listing only), so a
            # bucketless target cannot resolve to anything to delete; reject it
            # up front, without depending on the client validating.
            raise ValidationError('Invalid bucket name "": rm requires a bucket', operation="rm")
        root = rm_filter_root(storage.key, recursive=recursive)

        if not recursive and storage.key:
            self._rm_single(
                storage,
                filter,
                root=root,
                dryrun=dryrun,
                request_payer=request_payer,
                capture_response=capture_response,
                on_result=on_result,
                cancel_token=cancel_token,
                transfer_config=config,
            )
            _raise_if_cancelled(cancel_token, "rm")
            return

        # Enumerating paths: full recursive delete, or the keyless
        # non-recursive folder-marker sweep. Both list without Delimiter.
        # ScanOptions.prefix re-anchors the listing at the normalized prefix on
        # the passed storage itself (client shared), so a custom S3Storage
        # subclass and its scan_pages override survive; page_size / fetch_owner
        # come from the storage's own config via default_scan_options.
        options = replace(
            storage.default_scan_options(),
            recursive=True,
            request_payer=request_payer,
            prefix=root,
            filter=self._rm_scan_filter(filter, sweep=not recursive, base_key=storage.key),
            reusable_after_interrupt=self._reusable_after_interrupt,
        )

        if dryrun:
            # A lazily-deferred client is built ahead of the listing's
            # attribution, like the deleter's eager build below (and ls's).
            storage.get_client()
            # cancel_token on the scan too (like ls): the prefetch producer
            # stops between page pulls instead of fetching pages nobody reads.
            entries = storage.scan(options, cancel_token=cancel_token)
            with _scan_teardown(entries, reusable_after_interrupt=self._reusable_after_interrupt):
                # the listing's unnamed failure is this rm's; on_result's is not
                for info in _attributed_entries(entries, "rm"):
                    _raise_if_cancelled(cancel_token, "rm")
                    _emit_result(on_result, info=info, storage=storage, outcome=OpOutcome.DRYRUN)
                    _raise_if_cancelled(cancel_token, "rm")
            _raise_if_cancelled(cancel_token, "rm")
            return

        with S3Deleter(
            storage,
            request_payer=request_payer,
            on_result=on_result,
            cancel_token=cancel_token,
            operation="rm",
            capture_response=capture_response,
            transfer_config=config,
            session=self._crt.session,
            crt_endpoint=self._crt.endpoint,
            crt=self._crt.crt,
        ) as deleter:
            # cancel_token on the scan too (like ls): the prefetch producer
            # stops between page pulls instead of fetching pages nobody reads.
            entries = storage.scan(options, cancel_token=cancel_token)
            with _scan_teardown(entries, reusable_after_interrupt=self._reusable_after_interrupt):
                # the listing's unnamed failure is this rm's
                for info in _attributed_entries(entries, "rm"):
                    _raise_if_cancelled(cancel_token, "rm")
                    deleter.submit(info)
            _raise_if_cancelled(cancel_token, "rm")
        _raise_if_cancelled(cancel_token, "rm")
        if deleter.failed:
            raise BatchError(
                f"{deleter.failed} of {deleter.failed + deleter.succeeded} deletes failed",
                succeeded=deleter.succeeded,
                failed=deleter.failed,
                warned=0,
                skipped=0,
                operation="rm",
            ) from deleter.first_error

    @staticmethod
    def _rm_scan_filter(
        item_filter: FileFilter | None, *, sweep: bool, base_key: str
    ) -> FileFilter | None:
        """The ``ScanOptions.filter`` for rm's enumerating paths.

        The scan stamps each entry's ``Prefix``-relative ``compare_key``, so a
        glob/user filter reads it directly - shown aws-cli's own form of it where
        the typed ``base_key`` makes the two differ
        (`producers.s3_listing_filter`). The keyless sweep additionally
        restricts to folder markers (marker test first, then the user filter -
        aws-cli order). ``None`` when there is nothing to test, so unfiltered
        paths pay no per-object call. Evaluated page by page on the scan
        prefetch worker.
        """
        if not sweep:
            if item_filter is None:
                return None
            return producers.s3_listing_filter(item_filter, base_key)
        if item_filter is None:
            return _is_folder_marker
        predicate = item_filter
        return lambda info: _is_folder_marker(info) and predicate(info)

    def _rm_single(
        self,
        storage: S3Storage,
        item_filter: FileFilter | None,
        *,
        root: str,
        dryrun: bool,
        request_payer: str | None,
        capture_response: bool,
        on_result: ResultCallback | None,
        cancel_token: CancelToken | None,
        transfer_config: TransferConfig | None,
    ) -> None:
        """The blind single-key path (no listing; aws-cli's ``_list_single_object``).

        No scan runs here, so the entry's ``compare_key`` (its key relative to
        the prefix returned by `rm_filter_root`, what a glob filter matches) is
        stamped on the hand-built ``FileInfo``. A token cancelled from the
        ``FAILED`` record's callback wins over the failure's ``BatchError``,
        the precedence the batched path and the transfers give it; the caller
        polls the token after a success or a dry run.

        The request rides the client the recursive route's would
        (`crt_delete_sender`): the storage's own ``delete`` on botocore, or a
        ``DeleteObject`` of the same shape on the CRT client.
        """
        key = storage.key
        info = S3FileInfo(key=key, compare_key=key[len(root) :], storage=storage)
        if item_filter is not None and not item_filter(info):
            return
        src = storage.as_text()
        if dryrun:
            _emit_result(on_result, info=info, storage=storage, outcome=OpOutcome.DRYRUN, src=src)
            return
        # A lazily-deferred client is built ahead of the request: failing to
        # build one is not this key failing to delete (no FAILED record, no
        # BatchError), and belongs to no operation - as on the recursive route,
        # where the deleter builds it eagerly.
        client = storage.get_client()
        # Built ahead of the request for the same reason: an engine that
        # cannot be built is not this key failing to delete.
        crt = crt_delete_sender(client, transfer_config, operation="rm", crt=self._crt)

        try:
            if crt is None:
                response = storage.delete(info, request_payer=request_payer)
            else:
                params: dict[str, Any] = {"Bucket": storage.bucket, "Key": key}
                if request_payer is not None:
                    params["RequestPayer"] = request_payer
                with s3_request(operation="rm", bucket=storage.bucket, key=key):
                    response = crt.call("DeleteObject", params)
        except AssertionError:
            raise  # a test double's guard or an invariant, never a request outcome
        except Exception as exc:
            # The single key is still one batch item (aws-cli counts it as a task
            # failure -> "delete failed:" + rc 1), so aggregate rather than
            # re-raising the category error. Whatever the request raised is
            # that failure - the delete's own translated error, or anything
            # else botocore threw from inside the call, wrapped by
            # request_failure the way the deleter wraps it (aws-cli's task records
            # both alike). A request that died without any response is no
            # exception: aws-cli's classic task loses it with no line at rc 0,
            # which is not copied (design/opresult.md).
            failure = request_failure(exc, operation="rm", bucket=storage.bucket, key=key)
            _emit_result(
                on_result,
                info=info,
                storage=storage,
                outcome=OpOutcome.FAILED,
                error=failure,
                src=src,
            )
            _raise_if_cancelled(cancel_token, "rm")
            raise BatchError(
                "1 of 1 deletes failed",
                succeeded=0,
                failed=1,
                warned=0,
                skipped=0,
                operation="rm",
            ) from failure
        except BaseException as exc:
            # An interrupt (Ctrl-C, a request to exit) leaves at once: the item
            # is CANCELLED, as an interrupted transfer item is, and the
            # interrupt reaches the caller. The request may still complete on
            # the server; `aws` waits for it and prints its line, which is not
            # copied (docs/cli/aws-differences.md).
            _emit_result(
                on_result,
                info=info,
                storage=storage,
                outcome=OpOutcome.CANCELLED,
                error=CancelledError(
                    str(exc) or repr(exc), operation="rm", bucket=storage.bucket, key=key
                ),
                src=src,
            )
            raise
        # capture_response surfaces the DeleteObject response (minus
        # ResponseMetadata) under extra_info["delete"], the same shape the batched
        # path reconstructs from a DeleteObjects entry.
        extra_info = {"delete": strip_response_metadata(response)} if capture_response else None
        _emit_result(
            on_result,
            info=info,
            storage=storage,
            outcome=OpOutcome.SUCCEEDED,
            extra_info=extra_info,
            src=src,
        )

    # -- bucket / signing -------------------------------------------------

    def mb(
        self,
        target: Location,
        *,
        tags: Sequence[tuple[str, str]] | Mapping[str, str] | None = None,
    ) -> None:
        """Create the bucket of ``target`` with ``aws s3 mb`` semantics.

        ``target`` is an ``"s3://bucket"`` URI string (scheme optional) or an
        ``S3Storage``; a key part is ignored - aws-cli's ``mb`` also keeps
        only the bucket of its path (the CLI layer owns aws-cli's strict path
        checks). The request is shaped the way aws-cli shapes it:
        ``LocationConstraint`` is the client's region unless that is
        ``us-east-1`` (sending it for us-east-1 is an error), a bucket name
        ending in ``-an`` selects the account-regional bucket namespace, and
        ``tags`` become ``CreateBucketConfiguration.Tags``. ``tags`` is a
        mapping or a sequence of ``(key, value)`` pairs; pairs preserve order
        and duplicate keys (passed through for the server to reject, which is
        what the CLI's repeated ``--tags`` parity rests on). An SDK whose S3
        model predates the namespace or the tags member (botocore 1.42.67 /
        1.39.2) refuses the request in its own parameter validation, before
        anything is sent: a ``ValidationError``. A single-call
        operation: failures raise their category error directly, never
        ``BatchError``, and whatever else the request raised from inside
        botocore is the base error carrying it as ``__cause__``
        (`_single_request`, design/exceptions.md section 4).
        """
        storage = self._resolve_s3_target(target, operation="mb")
        bucket = storage.bucket
        if not bucket:
            raise ValidationError('Invalid bucket name "": mb requires a bucket', operation="mb")
        params: dict[str, Any] = {"Bucket": bucket}
        if bucket.endswith("-an"):
            # aws-cli is_account_regional_namespace_bucket: the -an suffix
            # selects the account-regional bucket namespace.
            params["BucketNamespace"] = "account-regional"
        config: dict[str, Any] = {}
        region = storage.get_client().meta.region_name
        if region != "us-east-1":
            config["LocationConstraint"] = region
        bucket_tags = _bucket_tags(tags)
        if bucket_tags:
            config["Tags"] = bucket_tags
        if config:
            params["CreateBucketConfiguration"] = config
        with _single_request("mb", bucket=bucket):
            storage.get_client().create_bucket(**params)

    def rb(self, target: Location) -> None:
        """Delete the (empty) bucket of ``target`` with ``aws s3 rb`` semantics.

        ``target`` is an ``"s3://bucket"`` URI string (scheme optional) or an
        ``S3Storage``; a key part is ignored like `mb` (aws-cli rejects
        it, but at the CLI layer). The bucket must already be empty - there
        is no ``force`` parameter: aws-cli composes ``rb --force`` from a
        full ``rm --recursive`` plus the bucket delete at the command layer,
        and callers compose ``S3.rm(target, recursive=True)`` + ``S3.rb``
        the same way. A single-call operation: failures (``BucketNotEmpty``,
        ``NoSuchBucket``, ...) raise their category error directly, never
        ``BatchError`` (design/exceptions.md section 4).
        """
        storage = self._resolve_s3_target(target, operation="rb")
        bucket = storage.bucket
        if not bucket:
            raise ValidationError('Invalid bucket name "": rb requires a bucket', operation="rb")
        client = storage.get_client()  # built ahead of the request's attribution, like mb
        with _single_request("rb", bucket=bucket):
            client.delete_bucket(Bucket=bucket)

    def presign(
        self,
        target: Location,
        *,
        expires_in: int = 3600,
        method: Literal["get_object", "put_object"] = "get_object",
    ) -> str:
        """Return a presigned URL for ``target`` with ``aws s3 presign`` semantics.

        ``target`` is an ``"s3://bucket/key"`` URI string (scheme optional -
        aws-cli's presign also takes ``bucket/key``) or an ``S3Storage``.
        Signing is pure client-side computation: no request is sent, so
        neither bucket nor key existence is checked, and ``expires_in`` is
        not range-validated here (aws-cli passes any integer through; S3
        enforces its 604800-second maximum only when the URL is *used*) -
        though SigV4A (a multi-region access point) is signed by awscrt's
        signer, which refuses ``0`` and negative values with its own bare
        ``AssertionError``; that refusal is raised as `InvalidValueError`,
        the assertion kept as its ``__cause__``
        (docs/reference/operations/presign.md). An
        empty bucket or key fails botocore's client-side parameter validation
        -> `ValidationError`. ``method`` selects the signed operation -
        aws-cli only ever signs ``get_object``; ``put_object`` is this
        library's permissive superset, and any other value is refused with
        `ValidationError` before anything is signed (otherwise a client
        method taking Bucket and Key would sign an undocumented operation,
        and miss the SigV4 forcing below).

        The URL is the one ``aws s3 presign`` returns, however the client
        was built. A symmetric SigV4 URL is signed with botocore's pure-Python
        signer, as aws-cli's bundled botocore signs it: botocore left to
        itself downgrades a default client's presign to the deprecated SigV2
        in the regions that accept it, and signs with awscrt's signer, whose
        query order differs, whenever awscrt is importable (`_sigv4_presign`).
        The auth-scheme signers a directory bucket (``v4-s3express``) or an
        MRAP ARN (``s3v4a``) resolve to, and an unsigned client, are left as
        botocore chose them. The URL names the client's **own** regional
        host, as aws-cli's does, where botocore would name the global one
        (`_regional_presign`). Both are scoped to this call: no client is
        built, and the client's own configuration is untouched.
        """
        if method not in ("get_object", "put_object"):
            raise ValidationError(f"Invalid method value: {method!r}", operation="presign")
        storage = self._resolve_s3_target(target, operation="presign")
        client = storage.get_client()
        operation = "GetObject" if method == "get_object" else "PutObject"
        try:
            with _single_request("presign", bucket=storage.bucket, key=storage.key):
                with _sigv4_presign(client, operation, expires_in), _regional_presign(client):
                    return client.generate_presigned_url(
                        method,
                        Params={"Bucket": storage.bucket, "Key": storage.key},
                        ExpiresIn=expires_in,
                    )
        except AssertionError as exc:
            if expires_in > 0:
                raise
            # The CRT signer's own bare assertion on a non-positive expiry -
            # the one it is known to raise here; any other passes through.
            raise InvalidValueError(
                f"The CRT signer refuses a non-positive expires_in ({expires_in})",
                operation="presign",
                bucket=storage.bucket,
                key=storage.key,
            ) from exc

    def website(
        self,
        target: Location,
        *,
        index_document: str | None = None,
        error_document: str | None = None,
    ) -> None:
        """Set the bucket website configuration with ``aws s3 website`` semantics.

        ``target`` is an ``"s3://bucket"`` URI string (scheme optional) or an
        ``S3Storage``; a key part is ignored - the library is permissive, the
        CLI layer owns aws-cli's strict path handling (aws-cli passes the key *as
        part of the bucket name* and lets botocore reject it). The request is
        shaped the way aws-cli shapes it: ``--index-document`` becomes
        ``IndexDocument.Suffix``, ``--error-document`` becomes
        ``ErrorDocument.Key``, unset ones are omitted - and with neither set
        an **empty** configuration is sent, leaving the rejection to the
        server, exactly like aws. An empty bucket fails botocore's
        client-side parameter validation -> `ValidationError`. A
        single-call operation: failures raise their category error directly,
        never ``BatchError`` (design/exceptions.md section 4).
        """
        storage = self._resolve_s3_target(target, operation="website")
        config: WebsiteConfigurationTypeDef = {}
        if index_document is not None:
            config["IndexDocument"] = {"Suffix": index_document}
        if error_document is not None:
            config["ErrorDocument"] = {"Key": error_document}
        client = storage.get_client()  # built ahead of the request's attribution, like mb
        with _single_request("website", bucket=storage.bucket):
            client.put_bucket_website(Bucket=storage.bucket, WebsiteConfiguration=config)


# The request-context key `_sigv4_presign` marks a presign it signs itself
# with: its choose-signer answer sets it, its before-sign handler consumes it.
_SIGV4_PRESIGN_MARK = "boto3_s3_sigv4_presign"


@contextmanager
def _sigv4_presign(client: Any, operation: str, expires_in: int) -> Generator[None, None, None]:
    """Sign one presign with SigV4 the way aws-cli does, for the duration of the call.

    aws-cli's bundled botocore presigns with its pure-Python SigV4 query
    signer. Left to itself, the installed botocore differs twice: a default
    client (no explicit ``signature_version``) in a region that still accepts
    SigV2 downgrades the presign to the legacy hmacv1 SigV2 - a URL a
    SigV4-only bucket rejects - and with awscrt importable every symmetric
    SigV4 signer is the CRT one, which orders the query differently
    (``X-Amz-Expires`` after ``X-Amz-SignedHeaders``). Both decisions go
    through botocore's process-wide signer table, which this must not change
    (design/overview.md section 3, rule 1), so the URL is signed here instead.

    A ``choose-signer.s3.<operation>`` handler answers ``UNSIGNED`` for the
    two symmetric SigV4 query signers (``v4-query`` from the endpoint's auth
    scheme or a ``v4`` client, ``s3v4-query`` from an ``s3v4`` client),
    marking the request; it runs ahead of botocore's downgrade, which sits on
    the less specific ``choose-signer.s3``, and so ahead of any handler a
    caller registered there. botocore then signs nothing, and a
    ``before-sign.s3.<operation>`` handler registered last on that event
    (handlers on the less specific ``before-sign.s3`` / ``before-sign`` still
    follow it) applies
    ``S3SigV4QueryAuth`` to the marked request with the region, signing name
    and credentials botocore would have used: the endpoint's signing context
    first, the client's own otherwise. Every other signer is left as botocore
    chose it - an unsigned client stays unsigned (it carries no mark), and
    the ``s3express`` / SigV4a schemes keep their own signers. SigV2
    configured explicitly is out of scope (design/overview.md section 2).
    Both handlers are unregistered in a ``finally``: no client is built and
    the client's configuration is untouched. ``client._request_signer``'s
    credentials are a private attribute, read as botocore's own signer reads
    them. A client that does not expose botocore's event seam (a stand-in
    implementing only ``generate_presigned_url``) signs however it chooses.
    """
    events = getattr(getattr(client, "meta", None), "events", None)
    if events is None or not hasattr(events, "register_last"):
        yield
        return
    from botocore import UNSIGNED
    from botocore.auth import S3SigV4QueryAuth
    from botocore.exceptions import NoRegionError

    def _choose(signature_version: Any, context: dict[str, Any], **_kwargs: Any) -> Any:
        if signature_version not in ("v4-query", "s3v4-query"):
            return None
        context[_SIGV4_PRESIGN_MARK] = True
        return UNSIGNED

    def _sign(
        request: Any,
        signing_name: str,
        region_name: str | None,
        request_signer: Any,
        **_kwargs: Any,
    ) -> None:
        if not request.context.pop(_SIGV4_PRESIGN_MARK, False):
            return
        if region_name is None:
            # botocore's own signer refuses a region-less client the same way.
            raise NoRegionError()
        signing = request.context.get("signing", {})
        credentials = signing.get("request_credentials") or request_signer._credentials
        # None reaches the signer as botocore's own does, to fail there.
        frozen: Any = credentials.get_frozen_credentials() if credentials is not None else None
        S3SigV4QueryAuth(
            frozen,
            signing.get("signing_name", signing_name),
            signing.get("region", region_name),
            expires=expires_in,
        ).add_auth(request)

    choose_event = f"choose-signer.s3.{operation}"
    sign_event = f"before-sign.s3.{operation}"
    events.register(choose_event, _choose)
    events.register_last(sign_event, _sign)
    try:
        yield
    finally:
        events.unregister(choose_event, _choose)
        events.unregister(sign_event, _sign)


@contextmanager
def _regional_presign(client: Any) -> Generator[None, None, None]:
    """Keep a presign on the client's own regional host for the duration of one call.

    aws-cli's bundled botocore always presigns the resolved regional host.
    The installed botocore reaches a global ``bucket.s3.amazonaws.com`` two
    ways. It marks presign requests with a ``use_global_endpoint`` context
    flag and then resolves the endpoint with the region builtin replaced by
    ``aws-global``, so an eu-central-1 client presigns the global host while
    every real request it sends goes to the regional one - and the URL still
    carries the true region in its credential scope, so host and signature
    disagree. And a us-east-1 client whose ``us_east_1_regional_endpoint`` is
    ``legacy`` (botocore's default; aws-cli has no such setting) carries the
    ``AWS::S3::UseGlobalEndpoint`` builtin from its construction. A handler
    registered *first* on ``before-endpoint-resolution.s3`` clears the flag
    before botocore's own builtins handler reads it, and sets that builtin to
    ``False``; botocore hands the event a copy of the builtins, so the
    client's own requests keep their host (design/overview.md section 3: a
    value an operation returns is matched to aws-cli however the client was
    built). Unregistered in a ``finally``.

    Outside us-east-1, everything botocore resolves regionally already is
    untouched: a non-``aws`` partition, dualstack, an explicit ``virtual`` or
    ``path`` ``addressing_style``, an ARN bucket, a directory bucket, and a
    DNS-incompatible name that forces path style; a legacy us-east-1 client
    moves to the regional host in path style too. A custom ``endpoint_url``
    overrides the resolved host either way (all measured against the pinned
    aws-cli). The host follows the client's region: a client built with no
    region configured is a us-east-1 one in botocore and gets its regional
    host, where aws-cli without a region presigns ``aws-global``'s global one
    (`S3.from_aws_cli` resolves the region as aws-cli does).

    ``use_global_endpoint`` is a botocore-internal context key; if a botocore
    version renames or drops it this override simply no-ops (the ``pop`` finds
    nothing), and a client without botocore's event seam is left alone, like
    `_sigv4_presign`.
    """
    events = getattr(getattr(client, "meta", None), "events", None)
    if events is None or not hasattr(events, "register_first"):
        yield
        return

    def _regional_endpoint(
        builtins: dict[str, Any], context: dict[str, Any], **_kwargs: Any
    ) -> None:
        context.pop("use_global_endpoint", None)
        builtins["AWS::S3::UseGlobalEndpoint"] = False

    event = "before-endpoint-resolution.s3"
    events.register_first(event, _regional_endpoint)
    try:
        yield
    finally:
        events.unregister(event, _regional_endpoint)


# -- module-level convenience -------------------------------------------------
# Thin wrappers over a default ``S3()`` for the common zero-config case
# (``boto3_s3.cp(...)`` rather than ``S3().cp(...)``), in the spirit of requests'
# module-level functions. ``Concatenate[S3, _P]`` strips ``self`` so each wrapper
# keeps its method's exact signature for type checkers and IDEs - no
# ``*args, **kwargs`` erasure - with the method itself as the single source of truth.
_P = ParamSpec("_P")
_R = TypeVar("_R")


def _delegate(method: Callable[Concatenate[S3, _P], _R]) -> Callable[_P, _R]:
    @functools.wraps(method)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        return method(S3(), *args, **kwargs)

    # ``functools.wraps`` sets ``__wrapped__``, which would make
    # ``inspect.signature`` report the method's signature *with* ``self``. Pin
    # the self-stripped signature explicitly (it wins over ``__wrapped__``) so
    # runtime introspection matches the documented contract - the method's
    # exact signature minus ``self`` (design/s3.md) - for help generators and
    # ``Signature.bind`` consumers, not just for type checkers.
    method_signature = inspect.signature(method)
    wrapper.__signature__ = method_signature.replace(  # pyright: ignore[reportAttributeAccessIssue]
        parameters=list(method_signature.parameters.values())[1:]
    )
    # ``functools.wraps`` also copied ``__qualname__`` (``S3.cp``), which is how
    # pickle locates a function by reference: the lookup would land on the
    # method itself and refuse the wrapper as "not the same object", so the
    # wrappers could not be handed to a ProcessPoolExecutor or multiprocessing.
    # Name the wrapper by its own module-level binding, where the lookup finds it.
    wrapper.__qualname__ = method.__name__
    return wrapper


cp = _delegate(S3.cp)
ls = _delegate(S3.ls)
mv = _delegate(S3.mv)
rm = _delegate(S3.rm)
mb = _delegate(S3.mb)
rb = _delegate(S3.rb)
presign = _delegate(S3.presign)
sync = _delegate(S3.sync)
website = _delegate(S3.website)


__all__ = [
    "S3",
    "cp",
    "ls",
    "mb",
    "mv",
    "presign",
    "rb",
    "rm",
    "rm_filter_root",
    "sync",
    "website",
]
