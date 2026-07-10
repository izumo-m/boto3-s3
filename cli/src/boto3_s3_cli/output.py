"""``aws s3`` output formatting (``ls`` listing, ``rm`` delete, ``mb`` / ``rb`` bucket lines).

Console output identity is not contractual (``docs/aws-cli-option-handling.md``
section 6), but the layout is kept close to aws-cli so tooling that parses its output
keeps working.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from boto3_s3 import FileKind

if TYPE_CHECKING:
    from datetime import datetime

    from boto3_s3 import FileInfo

_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_SIZE_WIDTH = 10
_PRE_WIDTH = 30
_HUMANIZE_SUFFIXES = ("KiB", "MiB", "GiB", "TiB", "PiB", "EiB")


def human_readable_size(size: float) -> str:
    """Format a byte count like aws-cli's ``--human-readable`` (base-2 units).

    Accepts a float so the transfer progress display can feed it byte-per-
    second rates (aws renders both through one helper, ``utils.py``).
    """
    base = 1024
    value = float(size)
    if size == 1:
        return "1 Byte"
    if value < base:
        return f"{int(value)} Bytes"
    for index, suffix in enumerate(_HUMANIZE_SUFFIXES):
        unit = base ** (index + 2)
        if round((value / unit) * base) < base:
            return f"{base * value / unit:.1f} {suffix}"
    return f"{value / base**6:.1f} {_HUMANIZE_SUFFIXES[-1]}"


def _size_str(size: int, *, human_readable: bool) -> str:
    return human_readable_size(size) if human_readable else str(size)


def _date_str(mtime: datetime | None) -> str:
    return mtime.astimezone().strftime(_DATE_FORMAT) if mtime is not None else " " * 19


def format_entry(info: FileInfo, *, recursive: bool, human_readable: bool) -> str:
    """Format one listing entry to match ``aws s3 ls``.

    Directories (S3 common prefixes, non-recursive only) render as a right-padded
    ``PRE name/`` line; objects render as ``<date> <size> <name>`` where the name
    is the basename for a non-recursive listing and the full key when recursive;
    buckets render as ``<creation date> <name>`` with no size column.
    """
    if info.kind is FileKind.DIRECTORY:
        # aws-cli ListCommand._display_page: ``Prefix.split('/')[-2]`` - the last
        # path component of the common prefix, which always ends with the
        # delimiter ``/``. A prefix ending in ``//`` yields an empty component
        # rendered ``PRE /`` (matching aws), where ``rstrip`` would wrongly keep
        # the parent ("PRE a/").
        name = info.key.split("/")[-2]
        return f"{'PRE':>{_PRE_WIDTH}} {name}/"
    if info.kind is FileKind.BUCKET:
        return f"{_date_str(info.mtime)} {info.key}"
    name = info.key if recursive else info.key.rsplit("/", 1)[-1]
    size = _size_str(info.size or 0, human_readable=human_readable)
    return f"{_date_str(info.mtime)} {size:>{_SIZE_WIDTH}} {name}"


def format_summary(total_objects: int, total_size: int, *, human_readable: bool) -> str:
    """Render the ``--summarize`` footer (object count and total size)."""
    size = _size_str(total_size, human_readable=human_readable)
    return f"\nTotal Objects: {total_objects}\n{'Total Size: ':>15}{size}\n"


def format_delete(bucket: str, key: str, *, dryrun: bool) -> str:
    """One ``rm`` success line (aws-cli results.py SUCCESS/DRY_RUN_FORMAT)."""
    prefix = "(dryrun) " if dryrun else ""
    return f"{prefix}delete: s3://{bucket}/{key}"


def format_delete_failed(bucket: str, key: str, error: BaseException | None) -> str:
    """One ``rm`` per-key failure line (aws-cli results.py FAILURE_FORMAT)."""
    return f"delete failed: s3://{bucket}/{key} {error}"


def format_make_bucket(bucket: str) -> str:
    """The ``mb`` success line (aws-cli prints the bucket name, not the path)."""
    return f"make_bucket: {bucket}"


def format_make_bucket_failed(path: str, error: object) -> str:
    """The ``mb`` failure line (the original path argument, like aws-cli)."""
    return f"make_bucket failed: {path} {error}"


def format_remove_bucket(bucket: str) -> str:
    """The ``rb`` success line (bucket name only, like aws-cli)."""
    return f"remove_bucket: {bucket}"


def format_remove_bucket_failed(path: str, error: object) -> str:
    """The ``rb`` failure line (the original path argument, like aws-cli)."""
    return f"remove_bucket failed: {path} {error}"
