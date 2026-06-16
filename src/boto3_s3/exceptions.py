"""Public exception hierarchy for the boto3-s3 library."""


class Boto3S3Error(Exception):
    """Root of every exception raised from boto3-s3 public APIs."""

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


class TransportError(Boto3S3Error):
    """Network or local I/O failure (connection, timeout, OSError)."""


class ConfigurationError(Boto3S3Error):
    """Required configuration (credentials, profile, endpoint) is missing or unresolvable."""


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
    "NotFoundError",
    "TransportError",
    "ValidationError",
]
