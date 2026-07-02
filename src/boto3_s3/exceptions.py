"""Public exception hierarchy for the boto3-s3 library."""


class Boto3S3Error(Exception):
    """Root of every exception raised from boto3-s3 public APIs.

    Never raised directly for a known failure - every raise site uses one of
    the subclasses below, so ``except Boto3S3Error`` is the catch-all. Direct
    instances appear only where no classification exists: the error
    translators' last-resort fallback (``s3storage.translate_boto_error``) and
    the message envelope on WARNED / NOTICE ``OpResult`` records.
    """

    def __init__(
        self,
        message: str,
        *,
        operation: str | None = None,
        bucket: str | None = None,
        key: str | None = None,
    ) -> None:
        super().__init__(message)
        self.operation: str | None = operation
        self.bucket: str | None = bucket
        self.key: str | None = key


class AccessDeniedError(Boto3S3Error):
    """Caller lacks permission (S3 403, local PermissionError)."""


class NotFoundError(Boto3S3Error):
    """Target resource does not exist (S3 404, local FileNotFoundError)."""


class ValidationError(Boto3S3Error):
    """Caller-supplied value, precondition, or state coherence is invalid."""


class InvalidValueError(ValidationError):
    """A caller-supplied value fails post-parse conversion or validation.

    aws-cli converts these *after* argument parsing (a bare ``int()`` on a
    ``cli_type_name: integer`` option, its timeout session handler), so the
    ``ValueError`` reaches its general exception handler (rc 255) - not the
    rc-252 usage path a parse-time rejection takes. The CLI's exit-code
    mapping keys on this subclass to preserve that distinction; library
    consumers can still catch it as a :class:`ValidationError`.
    """


class TransportError(Boto3S3Error):
    """Network or local I/O failure (connection, timeout, OSError)."""


class ConfigurationError(Boto3S3Error):
    """Required configuration is missing or unresolvable.

    Raised plainly for the failures aws gives dedicated handlers (rc 253):
    unresolvable credentials / region - and for an environment lacking a
    required capability (an SDK floor, an absent awscrt). A configuration
    that is *present but invalid* is the :class:`InvalidConfigError`
    refinement below.
    """


class InvalidConfigError(ConfigurationError):
    """A configuration value is present but invalid or unusable.

    The counterpart of aws-cli's ``InvalidConfigError``: a bad ``[s3]``
    config value, an unusable profile, partial credentials. aws reports
    these through its general exception handler (rc 255) - unlike the
    unresolvable credentials / region pair, whose dedicated handlers exit
    253 (plain :class:`ConfigurationError` here). The CLI's exit-code
    mapping keys on this subclass to preserve that distinction; library
    consumers can still catch it as a :class:`ConfigurationError`.
    """


class CancelledError(Boto3S3Error):
    """Operation was cancelled by the caller (e.g., via CancelToken.cancel())."""


class BatchError(Boto3S3Error):
    """Raised once at the end of a batch (``cp -r`` / ``mv -r`` / ``rm -r`` /
    ``sync``) when at least one item FAILED.

    Carries only rollup counts (O(1) memory regardless of item count); per-item
    detail is delivered live via the ``on_result`` hook. ``__cause__`` is the
    first failure encountered (a diagnostic sample, not a list).
    """

    def __init__(
        self,
        message: str,
        *,
        succeeded: int,
        failed: int,
        warned: int,
        skipped: int,
        operation: str | None = None,
    ) -> None:
        super().__init__(message, operation=operation)
        self.succeeded: int = succeeded
        self.failed: int = failed
        self.warned: int = warned
        self.skipped: int = skipped

    @property
    def total(self) -> int:
        """Items reaching the operation layer (succeeded + failed + warned + skipped)."""
        return self.succeeded + self.failed + self.warned + self.skipped


__all__ = [
    "AccessDeniedError",
    "BatchError",
    "Boto3S3Error",
    "CancelledError",
    "ConfigurationError",
    "InvalidConfigError",
    "InvalidValueError",
    "NotFoundError",
    "TransportError",
    "ValidationError",
]
