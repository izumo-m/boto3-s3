# mb

`mb` creates a bucket with `aws s3 mb` semantics: one `CreateBucket` request,
no enumeration, no per-item result stream. The contract shared by all nine
operations — accepted path forms, client selection, the module-level
wrappers — is in [`README.md`](./README.md); the narrative introduction is
[`../../library/README.md`](../../library/README.md), and the exception model
is [`../../library/errors.md`](../../library/errors.md).

## S3.mb

Creates the bucket of `target` and returns `None` on success. Only the bucket
part of the target is used: a key part is ignored, keeping the same part of
the path `aws s3 mb` keeps. The library is permissive about the rest of the
path form as well — the stricter checks the `boto3-s3` command applies, such
as its rejection of a scheme-less path, belong to the command, not to this
method.

Being a single-call operation, `mb` has no `dryrun`, no `on_result` and no
`cancel_token`: it produces no `OpResult` records and reports failure by
raising the category exception directly, never a `BatchError`.

```python
def mb(
    self,
    target: Location,
    *,
    tags: Sequence[tuple[str, str]] | Mapping[str, str] | None = None,
) -> None: ...
```

`target` names the bucket to create: an `"s3://bucket"` string (the `s3://`
scheme is optional, so `"bucket"` works too), an `os.PathLike[str]` yielding
such a string, or an [`S3Storage`](../storage.md#s3storage). A string or
`os.PathLike` becomes an `S3Storage` carrying this instance's `client()`; an
`S3Storage` is used as given, with its own client, building a default one if
it was constructed without one. Any other `Storage` — a `LocalStorage`, a
custom backend — is rejected, since the operation needs an S3 bucket and
client.

`tags` are the bucket's initial tags, sent as
`CreateBucketConfiguration.Tags`. A mapping is the convenience form. A
sequence of `(key, value)` pairs is passed through in order with duplicate
keys included: the library neither deduplicates nor validates them, leaving
the server to reject a duplicate key. The default `None`, an empty sequence
and an empty mapping are equivalent — no `Tags` is sent.

The request is shaped the way `aws s3 mb` shapes it. `CreateBucketConfiguration`
carries `LocationConstraint` set to the region of the client the operation
uses, except when that region is `us-east-1`, for which the constraint is
omitted (sending it for `us-east-1` is an error). A bucket name ending in
`-an` sets the top-level `BucketNamespace` request parameter to
`account-regional`, selecting the account-regional bucket namespace; it sits
alongside `CreateBucketConfiguration`, not inside it. When neither a location
constraint nor tags apply, no `CreateBucketConfiguration` is sent at all — a
`-an` bucket in `us-east-1` with no tags sends `BucketNamespace` and no
configuration block.

Raises:

- [`ValidationError`](../exceptions.md#validationerror) — `target` is neither
  a string, an `os.PathLike`, nor an `S3Storage`; the location is an
  unsupported ARN form or has a key but no bucket; or its bucket part is empty
  (`"s3://"`). No request is sent in these cases.
- [`ConfigurationError`](../exceptions.md#configurationerror), or its
  [`InvalidConfigError`](../exceptions.md#invalidconfigerror) refinement —
  a client had to be built and credentials, region, profile or endpoint were
  unresolvable. This covers a string or `os.PathLike` target, whose client is
  this instance's `client()`, and an `S3Storage` constructed without a
  `client`, which builds its default on first use; `mb` reads the location
  constraint from that client, so the failure precedes the request.
- The `CreateBucket` rejection itself, translated to its category:
  [`AccessDeniedError`](../exceptions.md#accessdeniederror),
  [`NotFoundError`](../exceptions.md#notfounderror),
  [`TransportError`](../exceptions.md#transporterror), or
  `ValidationError` for a 4xx other than 403/404 whose error code the
  category table does not list (`BucketAlreadyOwnedByYou`, for instance). The
  originating botocore `ClientError` is kept on `__cause__`. The full mapping
  is in [`../exceptions.md`](../exceptions.md).

## boto3_s3.mb

`boto3_s3.mb(...)` is the module-level convenience wrapper: it runs the method
above on a freshly built default `S3()`, and its signature is `S3.mb`'s minus
`self`. See [`README.md`](./README.md) for the shared wrapper contract.
