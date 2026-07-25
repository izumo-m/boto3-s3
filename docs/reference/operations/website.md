# website

`website` sets the static-website configuration of a bucket. It is one
`PutBucketWebsite` call and returns nothing on success. The narrative
introduction to the operation methods is in
[`../../library/s3-object.md`](../../library/s3-object.md); the contract the
operations share is in [`README.md`](./README.md).

## S3.website

Sends the website configuration assembled from `index_document` and
`error_document` to the bucket named by `target`. Returns `None`; success is
the absence of an exception.

```python
def website(
    self,
    target: Location,
    *,
    index_document: str | None = None,
    error_document: str | None = None,
) -> None
```

`target` names the bucket. It accepts `"s3://bucket"`, the same value without
the scheme (`"bucket"`), an `os.PathLike` producing either, or an `S3Storage`,
which is used as given, with its own client rather than this `S3` instance's —
the path forms the S3-only operations accept are specified in
[`README.md`](./README.md) and `S3Storage` itself in
[`../storage.md`](../storage.md). Only the bucket part is used: a key
(`"s3://bucket/some/key"`) is ignored rather than rejected, because the library
is the permissive layer and the strict path handling of `aws s3 website`
belongs to the `boto3-s3` command. A `LocalStorage`, any other `Storage`, and
any value that is neither a string, a path-like, nor an `S3Storage` are
rejected with `ValidationError`; so is a location that carries a key but no
bucket (`"s3:///some/key"`). An empty bucket name (`"s3://"`) reaches botocore
and fails its client-side parameter validation.

`index_document` is the suffix appended to a request for a directory-style
path, sent as `IndexDocument.Suffix` (`"index.html"` being the usual value).
`error_document` is the key of the document returned for a 4xx error, sent as
`ErrorDocument.Key`. Each defaults to `None`, meaning the corresponding element
is left out of the request entirely; the request carries exactly the elements
whose parameter was set, and never a value carried over from the bucket's
current configuration.

With both parameters left unset, an empty configuration is sent rather than the
call being refused locally: it passes botocore's client-side validation and the
service rejects it, which is what `aws s3 website` does as well. These two
settings are also the whole of what this operation expresses — redirect and
routing-rule configuration is not part of it.

`website` has no callback parameter and reports nothing to `on_result` (see
[`../results.md`](../results.md) for the callback model the batch operations
use). It also has no `dryrun` and no `cancel_token`: a single request either
succeeds or raises.

### Raises

The category contracts are specified in
[`../exceptions.md`](../exceptions.md).

- [`ValidationError`](../exceptions.md#validationerror) — `target` is not a
  supported type or is a non-S3 `Storage`; the resolved location is one of the
  forms the strict checks reject (an S3 Object Lambda ARN, an Outposts bucket
  ARN, a key with no bucket); botocore's client-side parameter validation
  rejects the request, which an empty bucket name produces; or the service
  answers with a 4xx other than 403 and 404 whose code is not in the category
  table — the class the service's rejection of an empty configuration arrives
  as.
- [`NotFoundError`](../exceptions.md#notfounderror) — the bucket does not
  exist (`NoSuchBucket`, or a 404 answer).
- [`AccessDeniedError`](../exceptions.md#accessdeniederror) — the caller lacks
  permission to set the configuration (`AccessDenied`, or a 403 answer).
- [`TransportError`](../exceptions.md#transporterror) — a 5xx or throttling
  answer, or a connection-level failure reaching the endpoint.
- [`ConfigurationError`](../exceptions.md#configurationerror) — credentials or
  region cannot be resolved while the client is built or the request is signed.
  Its [`InvalidConfigError`](../exceptions.md#invalidconfigerror) refinement
  covers configuration that is present but unusable, such as a set-but-unknown
  `AWS_PROFILE`, partial credentials, or a malformed `endpoint_url`.

`website` acts on one bucket and raises its category exception directly; it
never raises [`BatchError`](../exceptions.md#batcherror). The botocore error
behind the failure is kept as the raised exception's `__cause__`.

## boto3_s3.website

`boto3_s3.website(target, *, index_document=None, error_document=None)` is the
module-level convenience wrapper: it builds a default `S3()` and calls
`S3.website` on it, with the method's exact signature minus `self`. Use it for
the zero-config case; the wrappers are specified in
[`README.md`](./README.md).
