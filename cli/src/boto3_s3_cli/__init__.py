"""boto3-s3-cli - `aws s3` compatible CLI built on the boto3-s3 library."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    __version__: str

__all__ = ["__version__"]


def __getattr__(name: str) -> Any:
    """Resolve ``__version__`` on first access (PEP 562).

    importlib.metadata costs ~20ms to import; deferring it avoids that cost
    until the version is requested.
    """
    if name == "__version__":
        from importlib.metadata import PackageNotFoundError, version

        try:
            value = version("boto3-s3-cli")
        except PackageNotFoundError:  # pragma: no cover - only from an unbuilt checkout
            value = "0.0.0+unknown"
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
