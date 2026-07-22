"""The transfer planner: what a cp/mv/sync pair of paths means (aws-cli ``fileformat.py``).

The aws-cli pipeline that decides a path pair's shape is ported here - this
file plays aws-cli's ``fileformat.py``, holding the plan it produces:

- ``FileFormat.format``                  -> ``plan_transfer``. The per-side
  halves (``FileFormat.local_format`` / ``s3_format``, and aws-cli's
  ``CommandParameters._normalize_s3_trailing_slash``, which falls out of the
  S3 join) live on the backends as ``format``
  overrides, each computed from the endpoint's own held state.
- ``FileFormat.identify_type``           -> the CLI layer (string
  classification is its responsibility; the planner receives resolved
  ``Storage`` objects).
- ``utils.find_dest_path_comp_key``      -> ``item_paths`` / ``dest_for``
  (kept here rather than mirroring aws's ``utils`` home because they operate
  on the ``TransferPlan`` this module owns)

The planner sits *above* the storage backends: an endpoint arrives as a
resolved ``Storage``; its side is formatted by the
polymorphic ``Storage.format``, while the *route* is classified by concrete
type (``isinstance`` against ``S3Storage`` / ``LocalStorage``; any other
``Storage`` is a custom, ``open``-routed backend - the engine reaches into the
built-in classes' own API, so only they can take the built-in routes).
Importing this module therefore currently reaches ``botocore.exceptions``
through ``s3storage``. That timing is not part of the import contract.

S3 paths inside a ``TransferPlan`` use aws-cli's internal
``bucket/key`` form (scheme stripped); local paths are native (``os.sep``).
Building display strings (``s3://...`` / relative rendering) is the caller's
concern.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from boto3_s3.exceptions import ValidationError
from boto3_s3.localstorage import LocalStorage
from boto3_s3.s3storage import S3Storage

if TYPE_CHECKING:
    from boto3_s3.storage import Storage

PathsType = Literal["locals3", "s3local", "s3s3", "opens3", "s3open"]


@dataclass(frozen=True, slots=True, kw_only=True)
class TransferPlan:
    """The resolved shape of one cp/mv/sync path pair.

    ``src`` / ``dest`` are the resolved endpoint ``Storage`` objects this plan was
    built from, retained so the transfer can drive each side's own ``scan`` (and
    honor a ``Storage`` subclass override) instead of re-deriving the walk from
    the formatted root. ``src_root`` / ``dest_root`` are the *formatted* sides
    (aws-cli's ``FileFormat.format`` output): S3 in ``bucket/key`` form, local as
    a native absolute path, and a custom ``open`` side as ``""`` (its entries
    are addressed separately: a recursive item's ``open`` takes the relative
    ``compare_key``, a single item ``""`` - the location itself); directory
    semantics are expressed by a trailing separator on the built-in sides
    (the custom side's root stays bare ``""``, its directory-ness carried by
    ``use_src_name`` alone). ``--exclude`` / ``--include``
    need no source/destination path here: ``boto3_s3.globsieve`` matches a
    relative pattern against each item's ``compare_key`` and a root-anchored one
    against its full ``key`` at match time.
    """

    paths_type: PathsType
    dir_op: bool
    use_src_name: bool
    src: Storage
    dest: Storage
    src_root: str
    dest_root: str


def _paths_type(src: Storage, dest: Storage, *, operation: str) -> PathsType:
    """Validate the endpoint pairing and name its route (the plan's ``paths_type``).

    One structural match over the pair's concrete types (class patterns test
    ``isinstance``, subclasses included - not the ``scheme`` string: the s3
    route reaches into ``S3Storage``'s ``get_client``/``bucket``/``key`` and
    the local route into ``LocalStorage``'s ``path``, so only the concrete
    classes can take them). Any other ``Storage`` is a custom backend routed
    through ``Storage.open`` (``opens3`` / ``s3open``), which must pair with
    s3 - ``open`` to ``local``, ``open`` to ``open`` and ``local`` to ``local``
    have no ``aws s3`` route and are rejected (the CLI layer phrases the
    strict aws message itself). A stdio stream never reaches the built-in
    cases (``cp`` diverts it to the stream path up front), so it folds into
    the custom arm harmlessly.
    """
    match src, dest:
        case S3Storage(), S3Storage():
            return "s3s3"
        case LocalStorage(), S3Storage():
            return "locals3"
        case S3Storage(), LocalStorage():
            return "s3local"
        case LocalStorage(), LocalStorage():
            raise ValidationError(
                f"{operation} requires at least one s3:// path (local to local is not supported)",
                operation=operation,
            )
        # Below here one side is a custom (open-routed) backend: its bytes move
        # through ``Storage.open`` while the other side rides ``s3transfer``,
        # so it can only pair with s3 - never local, another custom backend,
        # or a stream.
        case _, S3Storage():
            return "opens3"
        case S3Storage(), _:
            return "s3open"
        case _:
            raise ValidationError(
                f"{operation}: a custom-backend path transfers only with an s3:// path "
                "(not local, another custom backend, or a stream)",
                operation=operation,
            )


def plan_transfer(
    src: Storage, dest: Storage, *, recursive: bool, operation: str = "cp"
) -> TransferPlan:
    """Format a cp/mv endpoint pair into a ``TransferPlan`` (aws-cli ``FileFormat.format``).

    The pairing is validated and its route named by ``_paths_type``; each
    side then formats *itself* - the polymorphic
    ``format`` (aws-cli's per-type
    ``local_format`` / ``s3_format``, computed from the endpoint's held state).
    Each side's separator stays readable as ``plan.src.sep`` /
    ``plan.dest.sep``. ``recursive`` is aws-cli's ``dir_op``.
    """
    paths_type = _paths_type(src, dest, operation=operation)
    src_root, _ = src.format(dir_op=recursive)
    # use_src_name is a destination-side property: whether the dest adopts the
    # source's name (aws-cli reads the same tuple slot of the dest side only).
    dest_root, use_src_name = dest.format(dir_op=recursive)

    return TransferPlan(
        paths_type=paths_type,
        dir_op=recursive,
        use_src_name=use_src_name,
        src=src,
        dest=dest,
        src_root=src_root,
        dest_root=dest_root,
    )


def dest_for(plan: TransferPlan, compare_key: str) -> str:
    """The destination path for an item's operation-relative ``compare_key``.

    The destination half of aws-cli's ``find_dest_path_comp_key``: the
    ``/``-separated ``compare_key`` is appended (separator-translated) to the
    destination root only when the destination adopts the source's name;
    otherwise the root stands alone. A producer-stamped ``FileInfo.compare_key``
    feeds this directly, so a transfer needs no re-derivation from the full key.
    """
    if plan.use_src_name:
        return plan.dest_root + compare_key.replace("/", plan.dest.sep)
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
    and call ``dest_for`` directly.
    """
    if plan.dir_op:
        rel_path = src_path[len(plan.src_root) :]
    else:
        rel_path = src_path.split(plan.src.sep)[-1]
    compare_key = rel_path.replace(plan.src.sep, "/")
    return dest_for(plan, compare_key), compare_key


__all__ = [
    "PathsType",
    "TransferPlan",
    "dest_for",
    "item_paths",
    "plan_transfer",
]
