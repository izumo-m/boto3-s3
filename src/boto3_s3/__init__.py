"""boto3-s3 - Python library providing `aws s3` operations through its own Python API.

The public surface below is re-exported lazily (PEP 562 module ``__getattr__``):
``import boto3_s3`` executes none of the submodules and none of the AWS SDK
(``boto3`` alone drags in ``s3transfer`` via its ``compat`` module, ~80ms).
Each symbol is imported on first attribute access instead, so a program pays
only for the operations it actually touches. The contract is pinned by
``tests/lib/test_import_contract.py``; the policy lives in ``docs/imports.md``.

Type checkers resolve the same names through the ``TYPE_CHECKING`` block, so
the laziness is invisible to them.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from boto3_s3 import globsieve
    from boto3_s3.comparator import (
        Comparator,
        PairFilter,
        ParallelCompare,
        SyncPair,
        all_of,
        any_of,
    )
    from boto3_s3.concurrency import prefetch
    from boto3_s3.deleter import S3Deleter
    from boto3_s3.exceptions import (
        AccessDeniedError,
        BatchError,
        Boto3S3Error,
        CancelledError,
        ConfigurationError,
        NotFoundError,
        TransportError,
        ValidationError,
    )
    from boto3_s3.globsieve import GlobFilter, GlobPattern
    from boto3_s3.localstorage import LocalStorage
    from boto3_s3.masking import set_stream_logger
    from boto3_s3.pathresolver import S3PathResolver, has_underlying_s3_path
    from boto3_s3.s3 import (
        S3,
        cp,
        ls,
        mb,
        mv,
        presign,
        rb,
        rm,
        rm_filter_root,
        sync,
        website,
    )
    from boto3_s3.s3storage import S3Storage
    from boto3_s3.storage import Location, Storage
    from boto3_s3.transferconfig import TransferConfig
    from boto3_s3.types import (
        CancelToken,
        CaseConflictMode,
        CopyPropsMode,
        FileFilter,
        FileInfo,
        FileKind,
        LocalFileInfo,
        OpKind,
        OpOutcome,
        OpResult,
        ProgressCallback,
        ResultCallback,
        S3FileInfo,
        ScanOptions,
        TransferOptions,
        TransferProgress,
    )

    __version__: str

__all__ = [
    "S3",
    "AccessDeniedError",
    "BatchError",
    "Boto3S3Error",
    "CancelToken",
    "CancelledError",
    "CaseConflictMode",
    "Comparator",
    "ConfigurationError",
    "CopyPropsMode",
    "FileFilter",
    "FileInfo",
    "FileKind",
    "GlobFilter",
    "GlobPattern",
    "LocalFileInfo",
    "LocalStorage",
    "Location",
    "NotFoundError",
    "OpKind",
    "OpOutcome",
    "OpResult",
    "PairFilter",
    "ParallelCompare",
    "ProgressCallback",
    "ResultCallback",
    "S3Deleter",
    "S3FileInfo",
    "S3PathResolver",
    "S3Storage",
    "ScanOptions",
    "Storage",
    "SyncPair",
    "TransferConfig",
    "TransferOptions",
    "TransferProgress",
    "TransportError",
    "ValidationError",
    "__version__",
    "all_of",
    "any_of",
    "cp",
    "globsieve",
    "has_underlying_s3_path",
    "ls",
    "mb",
    "mv",
    "prefetch",
    "presign",
    "rb",
    "rm",
    "rm_filter_root",
    "set_stream_logger",
    "sync",
    "website",
]

# Each public name's home module; ``__getattr__`` imports the module and pulls
# the attribute on first access. Must mirror the ``TYPE_CHECKING`` imports and
# ``__all__`` (``test_import_contract`` resolves every ``__all__`` entry, so
# drift fails the suite). ``globsieve`` (a submodule) and ``__version__``
# (metadata lookup) are resolved as special cases instead.
_EXPORT_HOMES: dict[str, str] = {
    "TransferConfig": "boto3_s3.transferconfig",
    "Comparator": "boto3_s3.comparator",
    "PairFilter": "boto3_s3.comparator",
    "ParallelCompare": "boto3_s3.comparator",
    "SyncPair": "boto3_s3.comparator",
    "all_of": "boto3_s3.comparator",
    "any_of": "boto3_s3.comparator",
    "prefetch": "boto3_s3.concurrency",
    "S3Deleter": "boto3_s3.deleter",
    "AccessDeniedError": "boto3_s3.exceptions",
    "BatchError": "boto3_s3.exceptions",
    "Boto3S3Error": "boto3_s3.exceptions",
    "CancelledError": "boto3_s3.exceptions",
    "ConfigurationError": "boto3_s3.exceptions",
    "NotFoundError": "boto3_s3.exceptions",
    "TransportError": "boto3_s3.exceptions",
    "ValidationError": "boto3_s3.exceptions",
    "GlobFilter": "boto3_s3.globsieve",
    "GlobPattern": "boto3_s3.globsieve",
    "LocalStorage": "boto3_s3.localstorage",
    "set_stream_logger": "boto3_s3.masking",
    "S3PathResolver": "boto3_s3.pathresolver",
    "has_underlying_s3_path": "boto3_s3.pathresolver",
    "S3": "boto3_s3.s3",
    "cp": "boto3_s3.s3",
    "ls": "boto3_s3.s3",
    "mv": "boto3_s3.s3",
    "rm": "boto3_s3.s3",
    "mb": "boto3_s3.s3",
    "rb": "boto3_s3.s3",
    "presign": "boto3_s3.s3",
    "sync": "boto3_s3.s3",
    "website": "boto3_s3.s3",
    "rm_filter_root": "boto3_s3.s3",
    "S3Storage": "boto3_s3.s3storage",
    "Location": "boto3_s3.storage",
    "Storage": "boto3_s3.storage",
    "CancelToken": "boto3_s3.types",
    "CaseConflictMode": "boto3_s3.types",
    "CopyPropsMode": "boto3_s3.types",
    "FileInfo": "boto3_s3.types",
    "FileKind": "boto3_s3.types",
    "LocalFileInfo": "boto3_s3.types",
    "OpKind": "boto3_s3.types",
    "OpOutcome": "boto3_s3.types",
    "OpResult": "boto3_s3.types",
    "ProgressCallback": "boto3_s3.types",
    "ResultCallback": "boto3_s3.types",
    "FileFilter": "boto3_s3.types",
    "S3FileInfo": "boto3_s3.types",
    "ScanOptions": "boto3_s3.types",
    "TransferOptions": "boto3_s3.types",
    "TransferProgress": "boto3_s3.types",
}


def _resolve_version() -> str:
    # importlib.metadata costs ~20ms to import, so only __version__ readers pay it.
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("boto3-s3")
    except PackageNotFoundError:  # pragma: no cover - only from an unbuilt checkout
        return "0.0.0+unknown"


def __getattr__(name: str) -> Any:
    """Resolve a public symbol on first access (PEP 562 lazy re-export)."""
    import importlib

    if name == "__version__":
        value: Any = _resolve_version()
    elif name == "globsieve":
        value = importlib.import_module("boto3_s3.globsieve")
    else:
        home = _EXPORT_HOMES.get(name)
        if home is None:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        value = getattr(importlib.import_module(home), name)
    globals()[name] = value  # cache: __getattr__ runs at most once per name
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
