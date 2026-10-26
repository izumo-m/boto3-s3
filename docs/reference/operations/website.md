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

`target` names the bucket. A string — or an `os.PathLike` producing one — is
read the way `aws s3 website` reads its argument: an `s3://` prefix and one
trailing `/` come off, and the rest is the bucket name whole, never split into
a bucket and a key. An S3 Object Lambda access point ARN and an Outposts
bucket ARN are rejected with `ValidationError` before any request, as
`aws s3` rejects them. Every other name goes to botocore as the bucket: an
access point ARN is one it takes, and a name it does not take — a key folded
in (`"s3://bucket/some/key"`), an empty name (`"s3://"`), a malformed ARN —
fails its client-side parameter validation with `ValidationError` before any
request, in botocore's words. An `S3Storage` is used as given, with its own
client rather than this `S3` instance's, and names its bucket
([`../storage.md`](../storage.md)). A `LocalStorage`, any other `Storage`, and
any value that is neither a string, a path-like, nor an `S3Storage` are
rejected with `ValidationError`.

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
- [`ConfigurationError`](../exceptions.md#configurationerror) — credentials
  cannot be resolved when the request is signed.
  Its [`InvalidConfigError`](../exceptions.md#invalidconfigerror) refinement
  covers configuration that is present but unusable, such as a set-but-unknown
  `AWS_PROFILE`, partial credentials, or a malformed region or `endpoint_url`.
  An S3 Express location the SDK predates raises `ConfigurationError` naming
  the version, before any request.

`website` acts on one bucket and raises its category exception directly; it
never raises [`BatchError`](../exceptions.md#batcherror). The botocore error
behind the failure is kept as the raised exception's `__cause__`. Any other
exception the `PutBucketWebsite` call raised from inside botocore — the
`FileNotFoundError` of a refreshing `credential_process` whose program is gone,
say — surfaces as the base [`Boto3S3Error`](../exceptions.md#boto3s3error) with the
original on `__cause__`, the same capture a per-item failure gets;
`AssertionError` alone is not translated.

## boto3_s3.website

`boto3_s3.website(target, *, index_document=None, error_document=None)` is the
module-level convenience wrapper: it builds a default `S3()` and calls
`S3.website` on it, with the method's exact signature minus `self`. Use it for
the zero-config case; the wrappers are specified in
[`README.md`](./README.md).
