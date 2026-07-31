# rb

`rb` deletes an empty bucket with `aws s3 rb` semantics: one `DeleteBucket`
request, no enumeration, no per-item result stream. The contract shared by all
nine operations — accepted path forms, client selection, the module-level
wrappers — is in [`README.md`](./README.md); the narrative introduction is
[`../../library/README.md`](../../library/README.md), and the exception model
is [`../../library/errors.md`](../../library/errors.md).

## S3.rb

Deletes the bucket of `target` and returns `None` on success. Only the bucket
part of the target is used: a key part is ignored, as in
[`mb`](./mb.md#s3mb). The library is permissive here; the strict
per-subcommand path rejections belong to the `boto3-s3` command, not to this
method.

The bucket must already be empty. There is no `force` parameter: emptying is a
separate operation, so a caller who wants it composes [`S3.rm`](./rm.md) with
`recursive=True` and then `S3.rb`, the same composition the command layer
performs for `rb --force`. Compose them on a target naming the bucket only —
`S3.rm("s3://bucket", recursive=True)` followed by `S3.rb("s3://bucket")` —
because `S3.rm` honors a key that `S3.rb` ignores: given
`"s3://bucket/prefix"` the recursive delete covers only what lies under
`prefix/`, and the `DeleteBucket` that follows still meets a non-empty
bucket.

Being a single-call operation, `rb` has no `dryrun`, no `on_result` and no
`cancel_token`: it produces no `OpResult` records and reports failure by
raising the category exception directly, never a `BatchError`.

```python
def rb(self, target: Location) -> None: ...
```

`target` names the bucket to delete: an `"s3://bucket"` string (the `s3://`
scheme is optional, so `"bucket"` works too), an `os.PathLike[str]` yielding
such a string, or an [`S3Storage`](../storage.md#s3storage). A string or
`os.PathLike` becomes an `S3Storage` carrying this instance's `client()`; an
`S3Storage` is used as given, with its own client, building a default one if
it was constructed without one. Any other `Storage` — a `LocalStorage`, a
custom backend — is rejected, since the operation needs an S3 bucket and
client.

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
  `client`, which builds its default on first use.
- The `DeleteBucket` rejection itself, translated to its category:
  `NoSuchBucket` becomes [`NotFoundError`](../exceptions.md#notfounderror), a
  403 becomes [`AccessDeniedError`](../exceptions.md#accessdeniederror), a 5xx
  or throttling response becomes
  [`TransportError`](../exceptions.md#transporterror), and a code the category
  table does not list — `BucketNotEmpty`, for instance — is widened by HTTP
  status, so a 4xx other than 403/404 surfaces as `ValidationError`. The
  originating botocore `ClientError` is kept on `__cause__`. The full mapping
  is in [`../exceptions.md`](../exceptions.md).

## boto3_s3.rb

`boto3_s3.rb(...)` is the module-level convenience wrapper: it runs the method
above on a freshly built default `S3()`, and its signature is `S3.rb`'s minus
`self`. See [`README.md`](./README.md) for the shared wrapper contract.
