"""What the SDK raises, as the library's ``Boto3S3Error`` categories.

Every operation's requests fail through here: a ``ClientError`` by its S3
error code and HTTP status, botocore's credential, configuration, transport
and request-shape errors by their class, the CRT engine's failures by their
awscrt name, s3transfer's retry exhaustion, and what a client build raises
outside botocore's family (design/exceptions.md). `s3_errors` and
`s3_request` are the captures a request runs inside; `attribute_failure`
fills the operation and location a raiser could not know.

This module is SDK-backed by declaration: it imports botocore and s3transfer
at module top.
"""

from __future__ import annotations

import errno
import re
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    EndpointConnectionError,
    HTTPClientError,
    IncompleteReadError,
    MissingDependencyException,
    NoCredentialsError,
    NoRegionError,
    ParamValidationError,
    PartialCredentialsError,
    ProfileNotFound,
)
from botocore.exceptions import ConnectionError as BotoConnectionError
from botocore.exceptions import InvalidConfigError as BotocoreInvalidConfigError
from s3transfer.exceptions import RetriesExceededError

from boto3_s3.exceptions import (
    AccessDeniedError,
    BatchError,
    Boto3S3Error,
    ConfigurationError,
    InvalidConfigError,
    NotFoundError,
    TransportError,
    ValidationError,
)

# The unresolvable credentials/region pair keeps the plain ConfigurationError
# (aws's dedicated rc-253 handlers); the present-but-unusable configuration
# family below refines to InvalidConfigError (aws's general handler, rc 255) -
# the same split point as the CLI's clientfactory (design/exceptions.md section
# 3). Membership differs: the factory's construction-scoped catch maps every
# other BotoCoreError to InvalidConfigError, while this general translator
# lists only the known config shapes and keeps anything unlisted at the base.
# botocore's own InvalidConfigError family is one of those shapes: a
# source_profile loop, a missing services section, an empty CA bundle
# (`reject_empty_ca_bundle`).
_UNRESOLVED_CONFIG_ERRORS: tuple[type[BaseException], ...] = (
    NoCredentialsError,
    NoRegionError,
)
_INVALID_CONFIG_ERRORS: tuple[type[BaseException], ...] = (
    PartialCredentialsError,
    ProfileNotFound,
    BotocoreInvalidConfigError,
)
# Both halves of botocore's transport tree: ConnectionError (endpoint /
# connect-timeout / proxy) and HTTPClientError (read timeout, a closed
# connection - "An HTTP Client raised an unhandled exception"). Beside them,
# IncompleteReadError: a body that ends short of its Content-Length is a
# transfer cut short like the others, but botocore files it directly under
# BotoCoreError - and only raises it itself under urllib3 1.x; urllib3 2
# enforces the length first and the same cut arrives as ResponseStreamingError,
# an HTTPClientError. Listed so one event has one category on both.
_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    EndpointConnectionError,
    BotoConnectionError,
    HTTPClientError,
    IncompleteReadError,
)

# The CRT engine's network and server failures by awscrt's error name: a
# connection, DNS, TLS or socket failure and S3's own 5xx / throttle answers
# the CRT client retried and gave up on - transport, as the classic engine's
# botocore files the same events. Every other name stays at the base here; a
# local file the engine could not open or write is the transfer engine's to
# classify (`transfer.Transferrer`), where the local path is known.
_CRT_TRANSPORT_NAMES = frozenset(
    {
        "AWS_ERROR_HTTP_CONNECTION_CLOSED",
        "AWS_ERROR_HTTP_SERVER_CLOSED",
        "AWS_ERROR_HTTP_RESPONSE_FIRST_BYTE_TIMEOUT",
        "AWS_ERROR_S3_SLOW_DOWN",
        "AWS_ERROR_S3_INTERNAL_ERROR",
    }
)
_CRT_TRANSPORT_PREFIXES = ("AWS_IO_SOCKET_", "AWS_IO_DNS_", "AWS_IO_TLS_")
_CRT_RUNTIME_ERROR = re.compile(r"\d+ \((AWS_[A-Z0-9_]+)\)")


def crt_error_name(exc: BaseException) -> str | None:
    """awscrt's error name for a CRT failure, else None.

    An ``AwsCrtError`` carries it (``name``, beside ``code``); a request awscrt
    refuses synchronously - a local file it cannot open - raises a plain
    ``RuntimeError("<code> (<name>): <text>")`` instead.
    """
    name: object = getattr(exc, "name", None) if hasattr(exc, "code") else None
    if not isinstance(name, str) and type(exc) is RuntimeError:
        match = _CRT_RUNTIME_ERROR.match(str(exc))
        name = match.group(1) if match else None
    return name if isinstance(name, str) and name.startswith("AWS_") else None


# S3 error Code -> exception category, shared by every translation path: the
# request-level ClientError below and the per-key DeleteObjects ``Errors[]``
# entries in deleter.py (which carry no HTTP status to widen on). The code
# states the intent more precisely than the status, so it is consulted first;
# status-based widening is the fallback.
S3_CODE_CATEGORIES: dict[str, type[Boto3S3Error]] = {
    "AccessDenied": AccessDeniedError,
    "NoSuchBucket": NotFoundError,
    "NoSuchKey": NotFoundError,
    "NoSuchVersion": NotFoundError,
    "NotFound": NotFoundError,
    "InternalError": TransportError,
    "SlowDown": TransportError,
    "ServiceUnavailable": TransportError,
    "RequestTimeout": TransportError,
}


def _translate_client_error(
    exc: ClientError, *, operation: str | None, bucket: str | None, key: str | None
) -> Boto3S3Error:
    """Map a botocore ``ClientError`` to the matching ``Boto3S3Error`` category.

    The error code is consulted first (``S3_CODE_CATEGORIES``); the HTTP
    status widens codes not in the table. The message is the ClientError's
    full str - the "An error occurred (...)" line aws-cli prints - so these
    read the same as the per-key delete failures synthesized in deleter.py.
    """
    response: Any = exc.response
    code: Any = response.get("Error", {}).get("Code", "")
    status: Any = response.get("ResponseMetadata", {}).get("HTTPStatusCode")

    category: type[Boto3S3Error] | None = S3_CODE_CATEGORIES.get(code)
    if category is None:
        if status == 403:
            category = AccessDeniedError
        elif status == 404:
            category = NotFoundError
        elif isinstance(status, int) and 500 <= status < 600:
            category = TransportError
        elif isinstance(status, int) and 400 <= status < 500:
            category = ValidationError
        else:
            category = Boto3S3Error
    return category(str(exc), operation=operation, bucket=bucket, key=key)


def translate_boto_error(
    exc: BaseException, *, operation: str | None, bucket: str | None = None, key: str | None = None
) -> Boto3S3Error:
    """Map any transfer-path exception to the matching ``Boto3S3Error``.

    ``ClientError`` goes through the code/status table; botocore's credential,
    transport, and request-shape errors map to their categories - unresolvable
    credentials/region and a missing optional dependency
    (``MissingDependencyException`` - awscrt absent where SigV4a signing needs
    it) to ``ConfigurationError``, a present-but-unusable
    profile, partial credentials or botocore's own ``InvalidConfigError``
    family to the ``InvalidConfigError`` refinement,
    and ``ParamValidationError`` (client-side validation - no HTTP happened)
    to ``ValidationError``, which aws-cli files under its usage rc. A CRT
    network or server failure is a ``TransportError`` by its awscrt name
    (`crt_error_name`), and so is s3transfer's ``RetriesExceededError``, the
    classic engine's download giving up on a body read. An existing ``Boto3S3Error`` passes through
    unchanged, and anything else becomes the base category carrying its
    message - an ``OSError`` included: raised inside a request it can be a
    credential provider's (a missing ``credential_process`` executable), not
    the filesystem the request is about, so a local file's failure is
    classified where the file is known (the transfer engine's records).
    """
    if isinstance(exc, Boto3S3Error):
        return exc
    if isinstance(exc, ClientError):
        return _translate_client_error(exc, operation=operation, bucket=bucket, key=key)
    if isinstance(exc, MissingDependencyException):
        # A missing optional dependency (awscrt: the SigV4a signing an MRAP
        # target needs, the CRT checksum family) is an environment problem,
        # so it keeps the plain ConfigurationError of the crt-absence family
        # (the CLI's `[s3] preferred_transfer_client = crt` degradation, rc
        # 253). The engine-selection path re-raises boto3's exception
        # unwrapped instead (crtsupport - the documented pass-through); that
        # exception never crosses this seam.
        return ConfigurationError(str(exc), operation=operation, bucket=bucket, key=key)
    if isinstance(exc, _UNRESOLVED_CONFIG_ERRORS):
        return ConfigurationError(str(exc), operation=operation, bucket=bucket, key=key)
    if isinstance(exc, _INVALID_CONFIG_ERRORS):
        return InvalidConfigError(str(exc), operation=operation, bucket=bucket, key=key)
    if isinstance(exc, _TRANSPORT_ERRORS):
        return TransportError(str(exc), operation=operation, bucket=bucket, key=key)
    if isinstance(exc, RetriesExceededError):
        # The classic engine's download gave up retrying a body read: s3transfer
        # retries only transport failures there (a read timeout, a dropped
        # connection, a body cut short of its Content-Length - its
        # S3_RETRYABLE_DOWNLOAD_ERRORS) and raises this, with aws-cli's "Max
        # Retries Exceeded", once its attempts run out. The same events are
        # TransportError on `get_file` and on the CRT engine.
        return TransportError(str(exc), operation=operation, bucket=bucket, key=key)
    if isinstance(exc, ParamValidationError):
        return ValidationError(str(exc), operation=operation, bucket=bucket, key=key)
    name = crt_error_name(exc)
    if name is not None and (
        name in _CRT_TRANSPORT_NAMES or name.startswith(_CRT_TRANSPORT_PREFIXES)
    ):
        return TransportError(str(exc), operation=operation, bucket=bucket, key=key)
    return Boto3S3Error(str(exc), operation=operation, bucket=bucket, key=key)


# The OSErrors a client build raises when a configured path cannot be used:
# a credential_process executable that is missing, not executable, a
# directory, or not a program at all.
_CONFIG_PATH_ERRORS = (FileNotFoundError, PermissionError, IsADirectoryError, NotADirectoryError)


def client_build_error(exc: ValueError | AttributeError | OSError) -> Boto3S3Error:
    """The family error for what the SDK raises outside its own family while it builds a client.

    It reads the configuration then: a malformed endpoint is a plain
    ``ValueError``, a config value of the wrong shape (``s3 = foo``, a scalar
    where it reads a section) an ``AttributeError``, and a configured path
    that cannot be used (a ``credential_process`` executable, run at build)
    an ``OSError`` - set-but-unusable configuration, `InvalidConfigError`.
    Any other ``OSError`` (out of file descriptors reading the SDK's own
    data) is local I/O failing, `TransportError`.
    """
    if isinstance(exc, OSError) and not (
        isinstance(exc, _CONFIG_PATH_ERRORS) or exc.errno == errno.ENOEXEC
    ):
        return TransportError(str(exc))
    return InvalidConfigError(str(exc))


@contextmanager
def s3_errors(
    *, operation: str | None, bucket: str | None = None, key: str | None = None
) -> Generator[None, None, None]:
    """Convert botocore errors from an S3 call into ``Boto3S3Error`` (keeping ``__cause__``)."""
    try:
        yield
    except (ClientError, BotoCoreError) as exc:
        raise translate_boto_error(exc, operation=operation, bucket=bucket, key=key) from exc


def attribute_failure(
    error: Boto3S3Error, *, operation: str | None, bucket: str | None, key: str | None
) -> None:
    """Fill the attribution a raiser could not know; never overwrite what it set.

    The one rule every operation-layer capture applies to a family error
    raised in-pipeline (design/exceptions.md): a storage's ``open`` /
    ``delete`` / ``validate`` leaves ``operation`` unset because the storage
    cannot know which operation is using it, so the operation fills it here.
    ``bucket`` and ``key`` fill only as a pair, never one alone: a raiser that
    named only ``key`` placed it in its own address space
    (`localstorage.translate_os_error`'s local path, which leaves ``bucket``
    unset by contract), and pairing this run's bucket with it would invent a
    mixed address. A ``BatchError`` is skipped outright - it stands for a
    whole run, and its coordinates are pinned as always-None
    (docs/reference/exceptions.md).
    """
    if error.operation is None:
        error.operation = operation
    if error.bucket is None and error.key is None and not isinstance(error, BatchError):
        error.bucket = bucket
        error.key = key


def request_failure(
    exc: Exception, *, operation: str | None, bucket: str | None = None, key: str | None = None
) -> Boto3S3Error:
    """The per-item failure for an exception a single request raised.

    `s3_errors` (or a backend's own translation) has already turned the boto
    family into a ``Boto3S3Error``, which passes through with its own cause
    and gets the attribution it left unset (`attribute_failure`). Anything
    else the call raised - a refreshing ``credential_process`` whose program
    is gone (``FileNotFoundError`` from botocore's ``Popen``) - is the
    request failing all the same, and becomes `translate_boto_error`'s
    last-resort ``Boto3S3Error`` (the exception's ``str()`` as the message)
    carrying the original as ``__cause__``. This is aws-cli's shape: its
    per-key ``DeleteObject`` runs as an s3transfer task, which records
    whatever the request raises as that key's failure. The
    transfer engine gets the same from s3transfer itself; the deleter, the
    blind single-key delete and sync's synchronous deletes apply it by hand,
    and the single-call operations and the storage's own requests - the
    listing, the HEAD, ``open`` / ``delete`` / ``get_file`` / ``put_file`` -
    through `s3_request`.
    """
    error = translate_boto_error(exc, operation=operation, bucket=bucket, key=key)
    if error is not exc:
        error.__cause__ = exc
    else:
        attribute_failure(error, operation=operation, bucket=bucket, key=key)
    return error


@contextmanager
def s3_request(
    *, operation: str | None, bucket: str | None = None, key: str | None = None
) -> Generator[None, None, None]:
    """Report whatever one S3 request raised as a ``Boto3S3Error``.

    `s3_errors` for the boto family, plus the `request_failure` capture for
    everything else the call raised from inside botocore - a refreshing
    ``credential_process`` whose program is gone (``FileNotFoundError``) -
    so ``except Boto3S3Error`` catches a failed request whatever botocore died
    of. Those become the base error carrying the original as ``__cause__`` and
    its ``str()`` as the message. A family error raised inside passes through
    untouched, and ``AssertionError`` alone is left untranslated (an invariant
    or a test double's guard, never a request outcome).

    Wrap the request only - the client call, or one step of a paginator
    (`request_pages`) - never the code that consumes its result: what that
    code raises is not a request failing.
    """
    try:
        with s3_errors(operation=operation, bucket=bucket, key=key):
            yield
    except (Boto3S3Error, AssertionError):
        raise
    except Exception as exc:
        raise request_failure(exc, operation=operation, bucket=bucket, key=key) from exc


__all__ = [
    "s3_errors",
    "translate_boto_error",
]
