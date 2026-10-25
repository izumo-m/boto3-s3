# Endpoint fidelity

Compatibility means boto3-s3 behaves like aws-cli against real S3 (the policy is
[`overview.md`](./overview.md) section 3). Running the suite against real S3
every time costs too much, so the e2e tier runs both CLIs against a local S3
server, the dev stack (RustFS, `scripts/compose.dev.yaml`), with occasional
real-AWS runs ([`testing.md`](./testing.md) section 3, endpoint policy).
Wherever the dev stack answers differently from real S3, a pass on it is no
evidence that the two agree on real S3. This file records those places
(section 1), every test-side change made because the endpoint is not
real S3 (section 2 - each must be revisited when the endpoint changes), and
how the dev stack's server was chosen (section 3).

## 1. The dev stack (RustFS) and moto against real S3

Every known way the dev stack answers differently from real S3, plus the
places where moto does and a golden is affected. The dev stack column is the
observed behavior of RustFS 1.0.3 (`rustfs/rustfs:1.0.3`, image revision
`331fb896`): probed on 1.0.2 on 2026-10-17 and again on 1.0.3, which keeps
1.0.2's server code, on 2026-10-25. The compose file pins that release, so a
newer one has not been checked against these rows until the probes are re-run;
`docker exec boto3-s3-dev-s3-1 rustfs --version` prints the running one.

The real-S3 column is measured where marked *measured* - by the real-AWS
runs of `tests/run_e2e.sh` in ap-northeast-1 (2026-10-15: 237 of 248 e2e
tests passed, all 11 failures test-side; 2026-10-17, after the section 2
fixes: 248 of 248) plus probes from those days - and is S3's documented
behavior otherwise; a row marked *unverified*
has never been checked against real S3. The moto column matters because
goldens replay on moto: a golden captured on one endpoint only works if moto
answers the same way. The last column points to the test-side changes in
section 2; the table doubles as the probe checklist for a new endpoint.

| # | Surface | Dev stack (RustFS 1.0.3) | Real S3 | moto | Section 2 |
|---|---|---|---|---|---|
| G1 | PutBucketWebsite without an IndexDocument (error document only, or empty) | rejected, `MalformedXML` | rejected (`InvalidArgument`; the error-document-only shape *unverified*) | stored | T8 |
| G2 | `--storage-class GLACIER` | `InvalidStorageClass`, rc 1 | accepted, rc 0 | accepted | T9 |
| G3 | Object annotations | ListObjectAnnotations `NotImplemented` | supported | not supported | T10 |
| G4 | CreateBucket on a bucket you own | succeeds (us-east-1 rule) | us-east-1 succeeds; other regions `BucketAlreadyOwnedByYou` | us-east-1 rule | T11 |
| G5 | `CreateBucketConfiguration.Tags` | accepted and ignored (empty tag set) | applied (*measured*) | ignored | T12 |
| G6 | Access points | none | supported | - | T13 |
| G7 | Keys with a `..` segment / `//` | PUT rejected (`InvalidArgument`, "Invalid object key") | accepted | - | T16 |
| G8 | Zero-byte PUT | the next PutObject with a body on the same pooled connection waits for the client's read timeout (HEAD, GET, and listings do not) | unaffected | unaffected | T1 |
| G9 | GetObjectAttributes, COMPOSITE checksum | value with the `-N` suffix, as HeadObject | general purpose buckets omit the suffix there (HeadObject has it); S3 Express directory buckets add it (*measured*) | - | none |
| G10 | UploadPartCopy (multipart copy) | destination carries no checksum | destination carries a CRC64NVME FULL_OBJECT checksum (*measured*) | - | none |
| G11 | PutObject with a canned ACL or grant, new bucket | accepted, rc 0 | new buckets have ACLs disabled (BucketOwnerEnforced): rejected, rc 1 on both CLIs (*measured*) | accepted | T5 |
| G12 | CopyObject with `If-None-Match: *` onto an existing key | honored, as real S3 | honored: neither CLI copies (*measured*) | condition ignored, the copy overwrites | T7 |
| G13 | Bucket create and delete as HeadBucket sees them | immediate | lags behind ListBuckets: a name queried before it existed answers 404 for about 30 s after the create; a bucket that held objects answers 200 for over 2 minutes after `rb --force`; a delete and re-create under one name can answer `BucketAlreadyOwnedByYou`. ListBuckets reflects a create and a delete at once (*measured*) | immediate | T4 |
| G14 | `LastModified` in listings | sub-second | whole seconds | - | none |
| G15 | PutObject without a Content-Type | guessed from the key (`application/json` for `.json`) | `binary/octet-stream` (*measured*) | `binary/octet-stream` | T17 |
| G16 | HeadObject checksum of a PutObject with the default checksum | the CRC32 value without `ChecksumType` | `ChecksumType: FULL_OBJECT` alongside the value | - | none |
| G17 | Transport of the dev stack (a setup property, not server behavior) | http | https | in-process | T2, T6 |
| G18 | ListBuckets with a `Prefix` | the prefix is ignored: every bucket is listed | only the matching buckets | - | none |

G14 changes no test - the e2e sync scenarios shift times by a day - but a
behavior that depends on it (`sync --exact-timestamps` converging) looks
different on the dev stack than on real S3. G17 matters because botocore
puts the flexible checksum into an aws-chunked trailer only over https, so
some failure paths differ by transport, and because aws-cli's CRT client takes
`use_ssl` from the command line only.

## 2. Test-side changes

Every change in `tests/`, `scripts/`, and `benchmarks/` that exists because
the endpoint is not real S3. "On a new endpoint" is what to do when the e2e endpoint changes,
to real S3 for a run or to another server for the dev stack. Changes to
`src/` and `cli/src/` are product fixes, not test-side changes, and are not
listed.

Each change carries the marker `endpoint-fidelity T<n>` where it lives - in
a comment, or in a scenario's `frozen_golden` text - so
`git grep -n "endpoint-fidelity T"` lists every site (T16, a rule, has
none). The forms a change takes:

- `diff_only` - no golden; only the live aws-cli-vs-ours comparison runs, rc
  included.
- `frozen_golden` - the committed golden matches real S3 and moto but not
  the dev stack, so the e2e tier neither rewrites nor drift-checks it; the
  moto replay and the live comparison still run.
- a live-only scenario, a narrowed probe or stderr token, a setup
  workaround, or a scenario left out.

| # | Change | Where | Gap | On a new endpoint |
|---|---|---|---|---|
| T1 | `Connection: close` on zero-byte PUTs | `harness.seed_bucket` | G8 | drop on real S3 or moto (harmless if kept) |
| T2 | the CRT lane passes `--endpoint-url` to both CLIs when `AWS_ENDPOINT_URL_S3` is set | `test_crt_parity._crt_invocation`, `benchmarks/e2e.py` (`_invoke`) | G17 | needed while the endpoint is http; `run_e2e.sh` unsets the variable, so real S3 skips it |
| T3 | sibling bucket names never end in `-an` | `e2e/conftest.py` (`_sibling_buckets`) | - | keep on every endpoint until account-regional BucketNamespace semantics are verified |
| T4 | every mb/rb scenario and side gets a sibling bucket of its own, never re-created under the same name; bucket existence is read from ListBuckets, and a bucket the account does not own is never named in a request before it exists; after a successful mb or rb the end state is read once the listing agrees (20 s at most) | `e2e/conftest.py` (`_sibling_buckets`), `test_mb_parity` / `test_rb_parity` (`_prepare`, `_settle`), `harness.bucket_exists` / `force_delete_bucket` / `settle_bucket_existence`, `run_e2e.sh` cleanup | G13 | needed on real S3; harmless elsewhere |
| T5 | `cp_upload_acl_public_read` / `cp_upload_grants_uri` are `diff_only` | `cp_scenarios.py` | G11 | stays while real S3 and moto disagree on ACLs for a new bucket |
| T6 | the aws-cli-side stderr token for a directory uploaded without `--recursive` is only `upload failed`: the cause after it is `Is a directory` over http, `Need to rewind the stream ...` over https | `cp_upload_dir_no_recursive` / `_exclude` in `cp_scenarios.py` | G17 | none: the token holds on every transport |
| T7 | `cp_no_overwrite_copy_exists` / `mv_no_overwrite_copy_exists` are `diff_only` | `cp_scenarios.py`, `mv_scenarios.py` | G12 | stays while moto ignores the condition |
| T8 | `website_error_only` / `website_no_options` are `diff_only` | `website_scenarios.py` | G1 | stays while moto stores a configuration without an IndexDocument |
| T9 | `cp_upload_storage_class_glacier` is `diff_only`; the glacier gate shapes run on moto | `cp_scenarios.py`; `TestCpGlacierOnMoto`, `TestMvGlacierOnMoto`, `TestSyncGlacierOnMoto` | G2 | the scenario can carry a golden on an endpoint that accepts GLACIER; keep the gate shapes on moto (restores take hours on real S3) |
| T10 | `cp_copy_props_all_multipart` is live-only | `cp_scenarios.py` | G3 | stays live-only: moto has no annotations either |
| T11 | `mb_existing` is `diff_only` and checks no stderr tokens | `mb_scenarios.py` | G4 | stays: the outcome depends on the region |
| T12 | `mb_tags` compares the tag set between aws-cli and ours only; no golden records a tag set | `mb_scenarios.py` | G5 | stays while moto ignores the tags |
| T13 | no e2e reaches an access point: `mv --validate-same-s3-paths` has no scenario, `ls_accesspoint_arn` is `diff_only` | `mv_scenarios.py`, `ls_scenarios.py` | G6 | real S3 could host an access point for `mv` (needs permissions and a cost estimate); `ls_accesspoint_arn` stays `diff_only` (a foreign ARN fails differently per endpoint) |
| T14 | `cp_copy_props_none` probes only `Metadata`, leaving the default `ContentType` unchecked | `cp_scenarios.py` | - | the dev stack and moto both answer `binary/octet-stream`; check real S3, then probe `ContentType` too |
| T15 | the dev stack itself: server and release, keys, us-east-1, the SSE-S3 master key, the readiness wait, the container name, the golden-regeneration hint, the benchmark guard | `scripts/compose.dev.yaml`, `compose-up.sh`, `dev-env.sh` / `.cmd`, `golden.py`, `benchmarks/awsenv.py` | - | rewrite for the new server; re-run the section 1 probes |
| T16 | no scenario uses `..` / `//` keys (the unit tier pins `..` bytes) | none: a rule for new scenarios, so no marker | G7 | keep the rule while the dev stack rejects such keys (the downloads were checked by hand instead: below) |
| T17 | the `cp_upload_no_guess_mime` golden is frozen | `cp_scenarios.py` | G15 | drop the freeze on an endpoint that answers `binary/octet-stream` |

What T16 leaves out was checked once outside the suite, on 2026-10-17. A
throwaway CloudServer 9.5.0 (`S3BACKEND=mem`), which accepts such keys
(section 3), served keys with `..`, `.`, and `\` segments: ones that climb
out of the prefix, ones that stay inside it, and ones that name the
destination or its parent. `cp --recursive` and `sync` downloaded them side
by side with the pinned `aws` on Linux and with `aws.exe` on Windows (this
command on Python 3.10 and 3.14). Exit codes, output, and the downloaded
tree matched on both systems. On Windows the `\` forms that climb out are
skipped like the `/` ones, and on neither system was anything written
outside the destination.

Not test-side changes, though they mention an endpoint: the `diff_only` of
the negative page-size scenarios (the reason is moto, which raises
`IndexError` for `MaxKeys=-1`; real S3 answers `InvalidArgument`); the
presign normalization (path-style URLs, `<ENDPOINT>`, `<REGION>`, and the
session token dropped from golden comparisons), which every endpoint needs
because capture and replay differ in host form and credentials; the
sorted rm lines, whose order comes from aws-cli's parallel completion;
nonexistent bucket names derived from the run's bucket (`<bucket>-no-such`),
which stay absent on real S3 too because `run_e2e.sh` randomizes the base;
the us-east-1 branch of `harness.create_bucket_in_region`, which is real
S3's rule; the expected values first observed on MinIO and later matched on
real S3 (`ls_page_size_zero`, `rm_page_size_zero`, `cp_checksum_sha256`);
`test_checksumcompare.TestCompositeForms`, whose bare COMPOSITE form is what
real S3 general purpose buckets answer;
the `measured on MinIO` notes in `tests/lib/` (where a client-side behavior
was measured, not a gap); and the example endpoints in unit tests (never
contacted).

## 3. Choosing the dev stack's server

Upstream `minio/minio` is archived, so the dev stack needs a maintained
server. A desk survey on 2026-10-06 listed the candidates below; on
2026-10-17 the three most likely were measured with the section 1 probes
and the e2e suite, and the dev stack moved to the closest one, RustFS.

### Measured on 2026-10-17

The dev stack ran `pgsty/minio` (`RELEASE.2026-08-04T00-00-00Z`), the
community fork of MinIO, until it was renamed `pgsty/silo` and stopped
receiving images. Silo, RustFS, and Zenko CloudServer then ran the section 1
probes and the e2e suite side by side. The goldens had been captured on
`pgsty/minio`, so an answer closer to real S3 can fail a golden.

| Surface | `pgsty/minio`, Silo `RELEASE.2026-10-15T00-00-00Z` | RustFS 1.0.2 | CloudServer 9.5.0 (`S3BACKEND=mem`) |
|---|---|---|---|
| PutBucketWebsite | `MalformedXML` on every put | as real S3 | as real S3 |
| SSE-S3 without a KMS | rejected | accepted once a master key is set | accepted |
| Re-creating a bucket you own (us-east-1) | `BucketAlreadyOwnedByYou` | succeeds, as real S3 | succeeds, as real S3 |
| Keys with `..` / `//` (G7) | rejected | rejected | accepted, as real S3 |
| Object `p` beside `p/x` | the listing hides `p/x` | two keys, as real S3 | two keys, as real S3 |
| Zero-byte PUT (G8) | the next PUT waits | the next PUT waits | no wait, as real S3 |
| COMPOSITE in GetObjectAttributes (G9) | as real S3 | `-N` suffix | not stored |
| UploadPartCopy checksum (G10) | none | none | as real S3 |
| CopyObject `If-None-Match` (G12) | ignored | honored, as real S3 | honored, as real S3 |
| PutObject `If-None-Match` / `If-Match` | honored, as real S3 (`If-Match` not probed on `pgsty/minio`) | honored, as real S3 | ignored |
| XXHASH and SHA512 checksums | `pgsty/minio` accepts; Silo rejects (`InvalidArgument`) | accepted, as real S3 | rejected |
| Default Content-Type (G15) | `binary/octet-stream`, as real S3 | guessed from the key | none |
| `CreateBucketConfiguration.Tags` (G5) | ignored | ignored | rejected (`MalformedXML`) |
| aws-cli's CRT client, download | works | works | writes no file |
| `REDUCED_REDUNDANCY` upload | works | works | leaves no object |
| e2e, 248 tests | `pgsty/minio` 248 passed; Silo 247 (the XXHASH64 rejection) | 244 passed; 3 failures were answers matching real S3 (G4, G12 twice), 1 was G15 | 238 passed, 10 failed: 3 as on RustFS, 7 answers further from real S3 |

All three reject GLACIER, lack object annotations, accept the CLI's ACL and
grant forms on a new bucket, and list `LastModified` to the sub-second. Silo and RustFS also
honor `If-Match` on DeleteObject, where `pgsty/minio` deleted anyway;
neither CLI sends it.

CloudServer drops out: without conditional PutObject the `--no-overwrite`
scenarios prove nothing, and aws-cli's CRT downloads fail against it. RustFS
answers as real S3 does on more surfaces than Silo and is further on two
(G9, G15), so it became the dev stack. It is pinned to 1.0.2 because its 1.0
release (2026-09-16) is recent.

### Desk survey (2026-10-06)

Facts marked *checked* were confirmed against the cited page or registry on
the survey date; *source* means read from the project's source tree at its
HEAD that day, runtime behavior unverified; everything else is unverified.

| Candidate | Status (survey date) | For this suite |
|---|---|---|
| **pgsty/silo** (AGPL-3.0) | releases 2026-08 to 2026-10; effectively one maintainer | Measured (above). A drop-in for `pgsty/minio`: the same `server` command, `MINIO_*` variables, and ports. |
| **Zenko CloudServer** (`ghcr.io/scality/cloudserver`, Apache-2.0) | 9.4.7 on 2026-10-05 (*checked*); maintained by Scality; Docker Hub `zenko/cloudserver` stale since 2023 | Measured (above): not suitable. |
| **Ceph RGW** (LGPL family) | Tentacle 20.2.4 / Squid 19.2.6 (2026-08-19) | The reference implementation s3-tests grew up with, but no light single container any more: `ceph/ceph-container` (the demo image) is archived; RGW without RADOS (`dbstore` / POSIX drivers) is marked experimental; SSE-S3 needs Vault. A candidate for an occasional reference run, not the everyday lane. |
| **RustFS** (Apache-2.0, CLA) | 1.0.0 GA 2026-09-16, 1.0.1 2026-10-03 (*checked*) | Measured (above): the dev stack since 2026-10-17. |
| VersityGW (Apache-2.0) | v1.8.0 2026-09-04 | Not suitable: SSE-S3 answers 501 (*source*); keys map onto file paths, so `..`, `//`, and `a` beside `a/b` likely diverge from S3. The only candidate that explicitly answers 501 for object annotations (*source*). |
| SeaweedFS (Apache-2.0) | 4.48 2026-09-28 | Not suitable: `NormalizeObjectKey` silently rewrites keys (`//` -> `/`, `\` -> `/`, leading `/` dropped; *checked*). |
| Garage (AGPL-3.0) | v2.4.1 2026-09-08 | Not suitable: no conditional PUT, no GetObjectAttributes, no SSE-S3 (*source* / its compatibility page). |
| s3proxy, moto server, Adobe S3Mock | - | Not suitable as the golden endpoint: missing website (s3proxy, S3Mock) or mocks that do not validate signatures. |

No candidate implements S3 object annotations; that surface stays on moto and
the library tests whichever endpoint is chosen.

### s3-tests numbers are not a ranking

The only cross-implementation s3-tests run found is Vitastor's 2024 comparison
(<https://vitastor.io/en/blog/2024-05-09-s3-comparison.html>, *checked*):
passed 576 (Ceph), 382 (CloudServer, file backend), 321 (MinIO), 56
(SeaweedFS), each "in the simplest configuration" and without excluding
`fails_on_aws`. s3-tests asserts Ceph RGW's behavior, and projects that publish
their own counts exclude different tag sets, so the counts screen candidates
but do not measure fidelity to real S3. This suite's own differential is the
measure that matters.
