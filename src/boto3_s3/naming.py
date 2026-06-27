"""Pure path-shape rules for transfers (aws-cli ``FileFormat`` parity).

The aws-cli pipeline that decides *what a cp/mv/sync pair of paths means* is
ported here as pure functions (no SDK imports), so the library and the CLI
derive identical shapes from one code path:

- ``fileformat.py``                      -> :func:`classify` / :func:`local_format` /
  :func:`s3_format` / :func:`plan_transfer`
- ``utils.find_bucket_key``              -> :func:`split_bucket_key` (ARN-aware)
- ``CommandParameters._normalize_s3_trailing_slash`` -> applied inside
  :func:`plan_transfer` (keyless ``s3://bucket`` reads as ``s3://bucket/``)
- ``utils.find_dest_path_comp_key``      -> :func:`item_paths`
- ``filters._get_s3_root`` / ``_get_local_root``     -> ``TransferPlan.filter_root``

S3 paths inside a :class:`TransferPlan` use aws-cli's internal
``bucket/key`` form (scheme stripped); local paths are native (``os.sep``).
Building display strings (``s3://...`` / relative rendering) is the caller's
concern.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from boto3_s3.exceptions import ValidationError

if TYPE_CHECKING:
    # Annotation-only (``from __future__ import annotations`` keeps it out of the
    # runtime import graph): the ``Storage`` ABC is SDK-free and does not import
    # ``naming``, so this is cycle-free and the pure-function layer stays SDK-free.
    from boto3_s3.storage import Storage

PathsType = Literal["locals3", "s3local", "s3s3", "opens3", "s3open"]
PathKind = Literal["s3", "local", "open"]

_S3_SCHEME = "s3://"

# ARN-shaped bucket parts, ported verbatim from aws-cli's ``find_bucket_key``
# (aws-cli's awscli/customizations/s3/utils.py). An ARN's resource part
# may itself contain "/", so these run before the plain first-"/" split; the
# whole ARN (group "bucket") is what the S3 API takes as ``Bucket``. The
# *rejected* ARN forms (Object Lambda, Outposts bucket) are s3storage's
# concern - this module only splits.
_S3_ACCESSPOINT_TO_BUCKET_KEY_RE = re.compile(
    r"^(?P<bucket>arn:(aws).*:s3:[a-z\-0-9]*:[0-9]{12}:accesspoint[:/][^/]+)/?(?P<key>.*)$"
)
_S3_OUTPOST_TO_BUCKET_KEY_RE = re.compile(
    r"^(?P<bucket>arn:(aws).*:s3-outposts:[a-z\-0-9]+:[0-9]{12}:outpost[/:]"
    r"[a-zA-Z0-9\-]{1,63}[/:]accesspoint[/:][a-zA-Z0-9\-]{1,63})[/:]?(?P<key>.*)$"
)


def classify(path: str) -> PathKind:
    """``"s3"`` iff the path starts with ``s3://`` - the only S3 marker aws knows."""
    return "s3" if path.startswith(_S3_SCHEME) else "local"


def relative_path(filename: str, start: str = os.path.curdir) -> str:
    """Render a local path relative to ``start`` (aws-cli's ``relative_path``).

    aws-cli splits first and joins the basename back on, so an in-tree
    path always carries a directory prefix (``./a.txt``, ``../x/a.txt``) -
    the form aws prints in transfer result lines and warnings. Where no
    relative path exists (different Windows drives), the absolute path is
    returned instead of raising.
    """
    try:
        dirname, basename = os.path.split(filename)
        relative_dir = os.path.relpath(dirname, start)
        return os.path.join(relative_dir, basename)
    except ValueError:
        return os.path.abspath(filename)


def split_bucket_key(path: str) -> tuple[str, str]:
    """Split a scheme-less S3 path into ``(bucket, key)`` (aws ``find_bucket_key``).

    Access-point ARNs (plain and Outposts) are recognized so the ARN - whose
    name may contain ``/`` - stays whole in ``bucket``. No validation happens
    here; either part may be empty.
    """
    for arn_re in (_S3_ACCESSPOINT_TO_BUCKET_KEY_RE, _S3_OUTPOST_TO_BUCKET_KEY_RE):
        match = arn_re.match(path)
        if match is not None:
            return match.group("bucket"), match.group("key")
    bucket, _, key = path.partition("/")
    return bucket, key


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
    their scan-root-relative ``compare_key`` - exactly what its ``open`` /
    ``get_fileinfo`` / ``delete`` take - so the root is always empty: the relative
    key passes straight through (:func:`dest_for` returns ``compare_key``
    unchanged, or ``""`` to mean the location itself). ``use_src_name`` mirrors
    :func:`s3_format`: a ``dir_op`` or an explicit trailing ``/`` means the
    destination adopts the source's name.
    """
    return "", (dir_op or text.endswith("/"))


def _strip_scheme_normalized(path: str) -> str:
    """Scheme-less form with the keyless-bucket normalization applied.

    aws-cli's ``_normalize_s3_trailing_slash`` runs on *every* path: a bucket-only
    path with no trailing slash (``s3://bucket``, including a keyless
    access-point ARN) reads as the bucket root ``s3://bucket/``. A bare
    ``s3://`` (service root) stays empty.
    """
    rest = path[len(_S3_SCHEME) :]
    _bucket, key = split_bucket_key(rest)
    if not key and rest and not rest.endswith("/"):
        return rest + "/"
    return rest


def _strip_scheme(path: str) -> str:
    """Drop a leading ``s3://`` if present (aws-cli's ``split_s3_bucket_key``)."""
    if path.startswith(_S3_SCHEME):
        return path[len(_S3_SCHEME) :]
    return path


def normalize_s3_uri(path: str) -> str:
    """The keyless-bucket normalization with the scheme kept.

    ``s3://bucket`` reads as ``s3://bucket/`` - the form aws validates and
    prints (``mv``'s same-path error shows the normalized URI).
    """
    return _S3_SCHEME + _strip_scheme_normalized(path)


def same_path(src: str, dest: str) -> bool:
    """Whether ``mv src dest`` would move an object onto itself.

    aws-cli's ``CommandParameters._same_path`` on two s3 URIs (the caller
    guarantees an s3->s3 pair): exact equality, or a ``/``-terminated
    destination whose ``basename(src)`` join reproduces ``src``. aws-cli
    runs this for ``--recursive`` too, so ``mv --recursive s3://b/p s3://b/``
    is rejected even though no key would map onto itself - a faithful
    false positive (rc 252). ``os.path`` is deliberate:
    aws-cli's own join/basename semantics are the contract.
    """
    if src == dest:
        return True
    if dest.endswith("/"):
        return src == os.path.join(dest, os.path.basename(src))
    return False


def same_key(src: str, dest: str) -> bool:
    """Whether the two s3 URIs name the same *key* (buckets ignored).

    aws-cli's ``CommandParameters._same_key``: the key parts are compared with
    the :func:`same_path` rule anchored at ``/`` - so a keyless destination
    matches any source whose key is its own basename. Gates ``mv``'s
    resolve-and-validate work and its access-point warning.
    """
    _, src_key = split_bucket_key(_strip_scheme(src))
    _, dest_key = split_bucket_key(_strip_scheme(dest))
    return same_path(f"/{src_key}", f"/{dest_key}")


@dataclass(frozen=True, slots=True, kw_only=True)
class TransferPlan:
    """The resolved shape of one cp/mv/sync path pair.

    ``src`` / ``dst`` are the resolved endpoint ``Storage`` objects this plan was
    built from, retained so the transfer can drive each side's own ``scan`` (and
    honor a ``Storage`` subclass override) instead of re-deriving the walk from
    the formatted root. ``src_root`` / ``dst_root`` are the *formatted* sides
    (aws-cli's ``FileFormat.format`` output): S3 in ``bucket/key`` form, local as
    a native absolute path, and a custom ``open`` side as ``""`` (it addresses
    entries by the relative ``compare_key`` its own ``open`` takes); directory
    semantics are expressed by a trailing separator. ``filter_root`` is what
    ``--exclude`` / ``--include`` patterns
    resolve against (aws-cli's ``filters._get_*_root``): for an S3 source the
    *key*-derived root (the bucket cancels out of the relative match, exactly
    like ``rm_filter_root``), for a local source an absolute directory; feed
    it to ``globsieve.translate_pattern_for_root`` and feed the resulting
    matcher each item's ``compare_key``.
    """

    paths_type: PathsType
    dir_op: bool
    use_src_name: bool
    src: Storage
    dst: Storage
    src_root: str
    dst_root: str
    src_sep: str
    dst_sep: str
    filter_root: str


def _endpoint_kind(storage: Storage) -> PathKind:
    """The transfer kind from a resolved endpoint's ``scheme`` (the object layer).

    ``"s3"`` / ``"local"`` are the built-in container pair, driven through
    ``s3transfer`` / the local path directly. Any other ``scheme`` is a custom
    backend, routed as ``"open"`` - its bytes move through ``Storage.open`` while
    the paired side (always s3) rides ``s3transfer``. A stdio stream never reaches
    here (``cp`` diverts it to the stream path up front), so its scheme folds into
    ``"open"`` harmlessly; an unsupported pairing is rejected by
    :func:`plan_transfer`, not here.
    """
    scheme = getattr(storage, "scheme", None)
    if scheme == "s3":
        return "s3"
    if scheme == "local":
        return "local"
    return "open"


def plan_transfer(
    src: Storage, dst: Storage, *, recursive: bool, operation: str = "cp"
) -> TransferPlan:
    """Format a cp/mv endpoint pair into a :class:`TransferPlan` (aws-cli ``FileFormat``).

    The route per side is read from each endpoint's ``scheme`` discriminator - the
    object layer, not a re-parsed scheme string - and the path shape from its
    ``as_text()``. ``"s3"`` / ``"local"`` are the built-in pair; any other scheme
    is a custom backend routed through ``Storage.open`` (``opens3`` / ``s3open``),
    which must pair with s3 - ``open`` to ``local``, ``open`` to ``open`` and
    ``local`` to ``local`` have no ``aws s3`` route and are rejected (the CLI layer
    phrases the strict aws message itself). ``recursive`` is aws-cli's ``dir_op``.
    """
    src_kind = _endpoint_kind(src)
    dst_kind = _endpoint_kind(dst)
    # A custom ``open`` side moves its bytes through ``Storage.open`` while the
    # other side rides ``s3transfer``, so it can only pair with s3 - never local,
    # another custom backend, or a stream.
    if "open" in (src_kind, dst_kind) and {src_kind, dst_kind} != {"open", "s3"}:
        raise ValidationError(
            f"{operation}: a custom-backend path transfers only with an s3:// path "
            "(not local, another custom backend, or a stream)",
            operation=operation,
        )
    if src_kind == "local" and dst_kind == "local":
        raise ValidationError(
            f"{operation} requires at least one s3:// path (local to local is not supported)",
            operation=operation,
        )

    src_text = src.as_text()
    dst_text = dst.as_text()
    if src_kind == "s3":
        src_rest = _strip_scheme_normalized(src_text)
        src_root = s3_format(src_rest, dir_op=recursive)[0]
        src_sep = "/"
        filter_root = _s3_filter_root(src_rest, dir_op=recursive)
    elif src_kind == "open":
        src_root = _open_format(src_text, dir_op=recursive)[0]
        src_sep = "/"
        filter_root = ""
    else:
        src_root = local_format(src_text, dir_op=recursive)[0]
        src_sep = os.sep
        filter_root = _local_filter_root(src_text, dir_op=recursive)

    if dst_kind == "s3":
        dst_root, use_src_name = s3_format(_strip_scheme_normalized(dst_text), dir_op=recursive)
        dst_sep = "/"
    elif dst_kind == "open":
        dst_root, use_src_name = _open_format(dst_text, dir_op=recursive)
        dst_sep = "/"
    else:
        dst_root, use_src_name = local_format(dst_text, dir_op=recursive)
        dst_sep = os.sep

    paths_type: PathsType
    if src_kind == "open":
        paths_type = "opens3"
    elif dst_kind == "open":
        paths_type = "s3open"
    elif src_kind == "local":
        paths_type = "locals3"
    elif dst_kind == "local":
        paths_type = "s3local"
    else:
        paths_type = "s3s3"
    return TransferPlan(
        paths_type=paths_type,
        dir_op=recursive,
        use_src_name=use_src_name,
        src=src,
        dst=dst,
        src_root=src_root,
        dst_root=dst_root,
        src_sep=src_sep,
        dst_sep=dst_sep,
        filter_root=filter_root,
    )


def _s3_filter_root(rest: str, *, dir_op: bool) -> str:
    """Key-derived filter root (aws-cli's ``_get_s3_root`` minus the bucket).

    Non-dir-op keys root at their parent prefix; the bucket segment cancels
    out of the relative comparison (``rm`` precedent), so only the key part
    is returned.
    """
    _bucket, key = split_bucket_key(rest)
    if not dir_op and not key.endswith("/"):
        key = "/".join(key.split("/")[:-1])
    return key


def _local_filter_root(path: str, *, dir_op: bool) -> str:
    """Absolute local filter root (aws-cli's ``_get_local_root``)."""
    if dir_op:
        return os.path.abspath(path)
    return os.path.abspath(os.path.dirname(path))


def dest_for(plan: TransferPlan, compare_key: str) -> str:
    """The destination path for an item from its root-relative ``compare_key``.

    The destination half of aws-cli's ``find_dest_path_comp_key``: the
    ``/``-separated ``compare_key`` is appended (separator-translated) to the
    destination root only when the destination adopts the source's name;
    otherwise the root stands alone. A producer-stamped ``FileInfo.compare_key``
    feeds this directly, so a transfer needs no re-derivation from the full key.
    """
    if plan.use_src_name:
        return plan.dst_root + compare_key.replace("/", plan.dst_sep)
    return plan.dst_root


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
    "normalize_s3_uri",
    "plan_transfer",
    "relative_path",
    "s3_format",
    "same_key",
    "same_path",
    "split_bucket_key",
]
