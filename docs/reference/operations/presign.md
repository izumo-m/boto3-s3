# presign

`presign` returns a presigned URL for one S3 object. For an ordinary bucket,
signing is a client-side computation over the location, the client's
credentials, and the call's arguments — no S3 request is made, so nothing
about the object is checked before the URL is handed back. An S3 Express
directory-bucket target is the carve-out: botocore fetches `CreateSession`
credentials while signing it, so that path does reach S3 and can fail there.
The narrative introduction to the operation methods is in
[`../../library/s3-object.md`](../../library/s3-object.md); the contract the
operations share is in [`README.md`](./README.md).

## S3.presign

Returns the presigned URL for the object named by `target`, valid for
`expires_in` seconds and authorizing the operation named by `method`. The URL
is returned exactly as botocore produced it.

```python
def presign(
    self,
    target: Location,
    *,
    expires_in: int = 3600,
    method: Literal["get_object", "put_object"] = "get_object",
) -> str
```

Where no S3 request is made, neither the bucket nor the key is checked for
existence and the caller's permissions are not consulted: a URL is produced
for an object that does not exist, and for one the caller cannot read. The
service decides both when the URL is used. Credential resolution still happens
during signing, so a credential provider that has to fetch something can
perform I/O of its own. A directory-bucket target goes further: the
`CreateSession` call its signer needs is a real S3 request against that
bucket, and its failure surfaces as a service-side category from `presign`
itself.

`target` names the object to sign. It accepts `"s3://bucket/key"`, the same
value without the scheme (`"bucket/key"`), an `os.PathLike` producing either,
or an `S3Storage`, which is used as given, with its own client rather than this
`S3` instance's — the path forms the S3-only operations accept are specified in
[`README.md`](./README.md) and `S3Storage` itself in
[`../storage.md`](../storage.md). A `LocalStorage`, any other `Storage`, and
any value that is neither a string, a path-like, nor an `S3Storage` are
rejected with `ValidationError`. Both a bucket and a key are needed in
practice: a target carrying only a bucket (`"s3://bucket"`, `"s3://bucket/"`)
and one carrying neither (`"s3://"`) each fail botocore's client-side parameter
validation.

`expires_in` is the number of seconds the URL stays valid, counted from the
moment it is signed; it defaults to `3600`. The value is stamped into the
signature and `presign` applies no range check of its own: a number above the
604800-second maximum S3 accepts is signed and returned, and S3 applies its
own limit only when the URL is presented. A non-positive value depends on the
signer botocore uses. With `awscrt` installed — the `crt` extra — signing goes
through the CRT signer, which rejects `0` and negative values with a bare
`AssertionError`; that is not a `Boto3S3Error` and is not translated into one.
Without `awscrt` the same values are signed and returned, carrying
`X-Amz-Expires=0` or a negative count.

`method` selects the operation the URL authorizes: `"get_object"`, the default,
signs a download URL, and `"put_object"` signs an upload URL. Those two values
are the supported set. `aws s3 presign` signs only `get_object`; `put_object`
is this library's permissive superset. Any other value raises `ValidationError`
(`Invalid method value: ...`) before the target is resolved or anything is
signed.

`presign` has no callback parameter and reports nothing to `on_result` (see
[`../results.md`](../results.md) for the callback model the batch operations
use); its result is the return value. It also has no `dryrun` and no
`cancel_token` — a single client-side computation has nothing to preview and no
window in which to observe cancellation.

### Signing

For a default client — one whose configuration sets no `signature_version`,
which is the state a plain boto3 client is in — against an ordinary bucket, the
returned URL is SigV4 query-signed: it carries
`X-Amz-Algorithm=AWS4-HMAC-SHA256` and an `X-Amz-Signature` parameter. The
observable difference from calling `generate_presigned_url` on the same client
directly is confined to the regions that accept SigV2 (`us-east-1` among
them), where stock botocore downgrades such a client's presigned URL to the
deprecated SigV2 query form (`AWSAccessKeyId` / `Signature` / `Expires`) — a
URL a SigV4-only bucket, or a bucket policy that requires SigV4, rejects. In
the remaining regions a default client already produces SigV4 and `presign`
changes nothing observable. `aws s3 presign` produces SigV4, and `presign`
matches it in every region.

The upgrade is confined to that one case; every other signer choice is left as
botocore made it:

- a client configured with an explicit `signature_version`, whether that is
  `s3v4` or a deliberately retained legacy `s3`;
- an unsigned client (`signature_version=botocore.UNSIGNED`), whose URL stays
  unsigned;
- an S3 Express directory bucket, for which botocore resolves the
  `sigv4-s3express` auth scheme. `presign` does not inspect `target` for the
  directory-bucket name shape and does not override the scheme botocore picked;
- a multi-region access point ARN, for which botocore resolves asymmetric
  SigV4a, likewise left in place;
- a client that does not expose botocore's event system at all — a stand-in
  implementing only `generate_presigned_url`, of the kind a `client()` override
  may return — which signs however it chooses.

No new client is built and the client's own configuration is not modified: the
client signs exactly as it did before once the call returns.

### Raises

The category contracts are specified in
[`../exceptions.md`](../exceptions.md).

- [`ValidationError`](../exceptions.md#validationerror) — a `method` outside the
  supported set (checked first, before the target is resolved); `target` is not
  a supported type or is a non-S3 `Storage`; the resolved location is one of the
  forms the strict checks reject (an S3 Object Lambda ARN, an Outposts bucket
  ARN, a key with no bucket); or botocore's client-side parameter validation
  rejects the request, which is what an absent key or an empty bucket name
  produces.
- [`ConfigurationError`](../exceptions.md#configurationerror) — credentials or
  region cannot be resolved, either while this `S3` builds its client or while
  the request is signed; or an optional dependency the resolved signer needs is
  absent, which is what a multi-region access point ARN produces when `awscrt`
  is not installed, its asymmetric SigV4a signing requiring it. Its
  [`InvalidConfigError`](../exceptions.md#invalidconfigerror) refinement covers
  configuration that is present but unusable, such as a set-but-unknown
  `AWS_PROFILE`, partial credentials, or a malformed `endpoint_url`.
- [`NotFoundError`](../exceptions.md#notfounderror),
  [`AccessDeniedError`](../exceptions.md#accessdeniederror) and
  [`TransportError`](../exceptions.md#transporterror) — on the directory-bucket
  signing path only, from the `CreateSession` call botocore makes to obtain the
  signing credentials: `NoSuchBucket` or a 404 answer, `AccessDenied` or a 403
  answer, and a 5xx or a failure to reach the endpoint, respectively.

`presign` acts on one object and raises its category exception directly; it
never raises [`BatchError`](../exceptions.md#batcherror). No path other than
the directory-bucket one sends an S3 request, so elsewhere the service-side
categories do not arise from the signing itself; any other botocore error
raised on the way is translated by the same category table.

## boto3_s3.presign

`boto3_s3.presign(target, *, expires_in=3600, method="get_object")` is the
module-level convenience wrapper: it builds a default `S3()` and calls
`S3.presign` on it, with the method's exact signature minus `self`. Use it for
the zero-config case; the wrappers are specified in
[`README.md`](./README.md).
