"""The transfer planner: what a cp/mv/sync pair of paths means (aws-cli ``fileformat.py``).

The aws-cli pipeline that decides a path pair's shape is ported here, module
for module - this file plays aws-cli's ``fileformat.py``:

- ``fileformat.py``                      -> :func:`classify` / :func:`local_format` /
  :func:`s3_format` / :func:`plan_transfer`
- ``CommandParameters._normalize_s3_trailing_slash`` -> applied inside
  :func:`plan_transfer` (keyless ``s3://bucket`` reads as ``s3://bucket/``;
  the string rule itself lives with the S3 grammar,
  :func:`boto3_s3.s3storage.strip_scheme_normalized`)
- ``utils.find_dest_path_comp_key``      -> :func:`item_paths` / :func:`dest_for`
  (kept here rather than mirroring aws's ``utils`` home because they operate
  on the :class:`TransferPlan` this module owns)

The planner sits *above* the storage backends: an endpoint arrives as a
resolved :class:`~boto3_s3.storage.Storage` and is routed by its concrete type
(``isinstance`` against ``S3Storage`` / ``LocalStorage``; any other ``Storage``
is a custom, ``open``-routed backend). Importing this module therefore reaches
``botocore.exceptions`` through ``s3storage`` (the dependency import contract
item 3 permits for the ``S3Storage`` surface); the per-backend string grammars
live on the backends themselves (``S3Storage.split_bucket_key`` and friends,
``LocalStorage.relative_path``).

S3 paths inside a :class:`TransferPlan` use aws-cli's internal
``bucket/key`` form (scheme stripped); local paths are native (``os.sep``).
Building display strings (``s3://...`` / relative rendering) is the caller's
concern.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from boto3_s3.exceptions import ValidationError
from boto3_s3.localstorage import LocalStorage
from boto3_s3.s3storage import S3Storage, strip_scheme_normalized

if TYPE_CHECKING:
    from boto3_s3.storage import Storage

PathsType = Literal["locals3", "s3local", "s3s3", "opens3", "s3open"]
PathKind = Literal["s3", "local", "open"]

_S3_SCHEME = "s3://"


def classify(path: str) -> PathKind:
    """``"s3"`` iff the path starts with ``s3://`` - the only S3 marker aws knows."""
    return "s3" if path.startswith(_S3_SCHEME) else "local"


def local_format(path: str, *, dir_op: bool) -> tuple[str, bool]:
    """Format one local path; return ``(formatted, use_src_name)``.

    aws-cli's ``FileFormat.local_format``: the path is absolutized; an existing
    directory, a ``dir_op``, or a user-typed trailing ``os.sep`` all mean
    "directory semantics" - the path gains a trailing ``os.sep`` and the
    destination side would take the source's name. Note the trailing-separator
    test runs on the *raw* input (``abspath`` strips it).
    """
    full_path = os.path.abspath(path)
    if (os.path.exists(full_path) and os.path.isdir(full_path)) or dir_op:
        return full_path + os.sep, True
    if path.endswith(os.sep):
        return full_path + os.sep, True
    return full_path, False


def s3_format(path: str, *, dir_op: bool) -> tuple[str, bool]:
    """Format one scheme-less S3 path; return ``(formatted, use_src_name)``.

    aws-cli's ``FileFormat.s3_format``: a ``dir_op`` path is ``/``-terminated and
    takes the source's name; otherwise only an explicit trailing ``/`` does.
    """
    if dir_op:
        if not path.endswith("/"):
            path += "/"
        return path, True
    return path, path.endswith("/")


def _open_format(text: str, *, dir_op: bool) -> tuple[str, bool]:
    """Format a custom-backend (``open``-routed) side; return ``(root, use_src_name)``.

    A custom ``Storage`` encapsulates its own location and addresses entries by
    their scan-root-relative ``compare_key``: its ``open`` / ``get_fileinfo``
    take that relative key, and ``delete`` takes the entry's ``FileInfo`` (whose
    ``key`` is that same relative key). So the root is always empty: the relative
    key passes straight through (:func:`dest_for` returns ``compare_key``
    unchanged, or ``""`` to mean the location itself). ``use_src_name`` mirrors
    :func:`s3_format`: a ``dir_op`` or an explicit trailing ``/`` means the
    destination adopts the source's name.
    """
    return "", (dir_op or text.endswith("/"))


@dataclass(frozen=True, slots=True, kw_only=True)
class TransferPlan:
    """The resolved shape of one cp/mv/sync path pair.

    ``src`` / ``dest`` are the resolved endpoint ``Storage`` objects this plan was
    built from, retained so the transfer can drive each side's own ``scan`` (and
    honor a ``Storage`` subclass override) instead of re-deriving the walk from
    the formatted root. ``src_root`` / ``dest_root`` are the *formatted* sides
    (aws-cli's ``FileFormat.format`` output): S3 in ``bucket/key`` form, local as
    a native absolute path, and a custom ``open`` side as ``""`` (it addresses
    entries by the relative ``compare_key`` its own ``open`` takes); directory
    semantics are expressed by a trailing separator. ``--exclude`` / ``--include``
    need no root here: :mod:`boto3_s3.globsieve` matches a relative pattern
    against each item's ``compare_key`` and a root-anchored one against its full
    ``key`` at match time.
    """

    paths_type: PathsType
    dir_op: bool
    use_src_name: bool
    src: Storage
    dest: Storage
    src_root: str
    dest_root: str
    src_sep: str
    dest_sep: str


def _endpoint_kind(storage: Storage) -> PathKind:
    """The transfer kind from a resolved endpoint's concrete type (the object layer).

    ``isinstance`` against the built-in pair, subclasses included - not the
    ``scheme`` string: the s3 route reaches into ``S3Storage``'s
    ``get_client``/``bucket``/``key`` and the local route into
    ``LocalStorage``'s ``path``, so only the concrete classes can take them
    (a ``Storage`` merely *claiming* ``scheme == "s3"`` could not). Any other
    ``Storage`` is a custom backend, routed as ``"open"`` - its bytes move
    through ``Storage.open`` while the paired side (always s3) rides
    ``s3transfer``. A stdio stream never reaches here (``cp`` diverts it to
    the stream path up front), so it folds into ``"open"`` harmlessly; an
    unsupported pairing is rejected by :func:`plan_transfer`, not here.
    """
    if isinstance(storage, S3Storage):
        return "s3"
    if isinstance(storage, LocalStorage):
        return "local"
    return "open"


def plan_transfer(
    src: Storage, dest: Storage, *, recursive: bool, operation: str = "cp"
) -> TransferPlan:
    """Format a cp/mv endpoint pair into a :class:`TransferPlan` (aws-cli ``FileFormat``).

    The route per side is read from each endpoint's concrete type - the
    object layer (:func:`_endpoint_kind`), not a re-parsed scheme string - and
    the path shape from its ``as_text()``. ``S3Storage`` / ``LocalStorage`` are
    the built-in pair; any other ``Storage``
    is a custom backend routed through ``Storage.open`` (``opens3`` / ``s3open``),
    which must pair with s3 - ``open`` to ``local``, ``open`` to ``open`` and
    ``local`` to ``local`` have no ``aws s3`` route and are rejected (the CLI layer
    phrases the strict aws message itself). ``recursive`` is aws-cli's ``dir_op``.
    """
    src_kind = _endpoint_kind(src)
    dest_kind = _endpoint_kind(dest)
    # A custom ``open`` side moves its bytes through ``Storage.open`` while the
    # other side rides ``s3transfer``, so it can only pair with s3 - never local,
    # another custom backend, or a stream.
    if "open" in (src_kind, dest_kind) and {src_kind, dest_kind} != {"open", "s3"}:
        raise ValidationError(
            f"{operation}: a custom-backend path transfers only with an s3:// path "
            "(not local, another custom backend, or a stream)",
            operation=operation,
        )
    if src_kind == "local" and dest_kind == "local":
        raise ValidationError(
            f"{operation} requires at least one s3:// path (local to local is not supported)",
            operation=operation,
        )

    src_text = src.as_text()
    dest_text = dest.as_text()
    if src_kind == "s3":
        src_root = s3_format(strip_scheme_normalized(src_text), dir_op=recursive)[0]
        src_sep = "/"
    elif src_kind == "open":
        src_root = _open_format(src_text, dir_op=recursive)[0]
        src_sep = "/"
    else:
        src_root = local_format(src_text, dir_op=recursive)[0]
        src_sep = os.sep

    if dest_kind == "s3":
        dest_root, use_src_name = s3_format(strip_scheme_normalized(dest_text), dir_op=recursive)
        dest_sep = "/"
    elif dest_kind == "open":
        dest_root, use_src_name = _open_format(dest_text, dir_op=recursive)
        dest_sep = "/"
    else:
        dest_root, use_src_name = local_format(dest_text, dir_op=recursive)
        dest_sep = os.sep

    paths_type: PathsType
    if src_kind == "open":
        paths_type = "opens3"
    elif dest_kind == "open":
        paths_type = "s3open"
    elif src_kind == "local":
        paths_type = "locals3"
    elif dest_kind == "local":
        paths_type = "s3local"
    else:
        paths_type = "s3s3"
    return TransferPlan(
        paths_type=paths_type,
        dir_op=recursive,
        use_src_name=use_src_name,
        src=src,
        dest=dest,
        src_root=src_root,
        dest_root=dest_root,
        src_sep=src_sep,
        dest_sep=dest_sep,
    )


def dest_for(plan: TransferPlan, compare_key: str) -> str:
    """The destination path for an item from its root-relative ``compare_key``.

    The destination half of aws-cli's ``find_dest_path_comp_key``: the
    ``/``-separated ``compare_key`` is appended (separator-translated) to the
    destination root only when the destination adopts the source's name;
    otherwise the root stands alone. A producer-stamped ``FileInfo.compare_key``
    feeds this directly, so a transfer needs no re-derivation from the full key.
    """
    if plan.use_src_name:
        return plan.dest_root + compare_key.replace("/", plan.dest_sep)
    return plan.dest_root


def item_paths(plan: TransferPlan, src_path: str) -> tuple[str, str]:
    """Derive one item's ``(dest_path, compare_key)`` from its source path.

    aws-cli's ``find_dest_path_comp_key``: a ``dir_op`` item is the source path
    relative to ``src_root``; a single item is the last source-separator
    component. ``compare_key`` is that relative part ``/``-separated - the
    name under which the item is filtered, reported, and (for sync) compared.
    The destination appends it (separator-translated) only when the
    destination takes the source's name. Used where no listing ``FileInfo`` is
    at hand (a stream's dest); the listing paths read ``FileInfo.compare_key``
    and call :func:`dest_for` directly.
    """
    if plan.dir_op:
        rel_path = src_path[len(plan.src_root) :]
    else:
        rel_path = src_path.split(plan.src_sep)[-1]
    compare_key = rel_path.replace(plan.src_sep, "/")
    return dest_for(plan, compare_key), compare_key


__all__ = [
    "PathKind",
    "PathsType",
    "TransferPlan",
    "classify",
    "dest_for",
    "item_paths",
    "local_format",
    "plan_transfer",
    "s3_format",
]
