# Endpoint fidelity

Parity means boto3-s3 behaves like aws against real S3. Running the suite
against real S3 every time costs too much, so the e2e tier runs both CLIs
against a local substitute (Silo, a MinIO fork) instead, with occasional
real-AWS runs ([`testing.md`](./testing.md) section 3, endpoint policy).
Wherever the
substitute answers differently from real S3, a pass on it is no evidence of
parity on real S3. This file records those places (section 1), every
test-side change made because the endpoint is not real S3 (section 2 - each
must be revisited when the endpoint is replaced), and the dev stack's image
with the candidates for a replacement (section 3).

## 1. The dev stack (Silo) against real S3

Every known way the dev stack (`pgsty/silo`, `scripts/compose.dev.yaml`)
answers differently from real S3, in one place. The Silo column is the
observed behavior of Silo `RELEASE.2026-10-15T00-00-00Z` (commit `7a3ca0f8`),
probed on 2026-10-17. The rows were first measured on its predecessor
`pgsty/minio` (last release `RELEASE.2026-08-04T00-00-00Z`); the same probes
gave the same answers on Silo except for G18 (G7 and G17 were not
re-probed). The compose file follows `pgsty/silo:latest`, so the running
release can move past the one above;
`docker exec boto3-s3-dev-minio-1 silo --version` prints it, and a newer
release has not been checked against these rows.

The real-S3 column is measured where marked *measured* - by the one dated
real-AWS run, `tests/run_e2e.sh` in ap-northeast-1 on 2026-10-15 (237 of 248
e2e tests passed; all 11 failures were test-side and are rows here) plus
probes from the same day - and is S3's documented behavior otherwise; a row
marked *unverified* has never been checked against real S3. The moto
column matters because goldens replay on moto: a golden re-captured on real
S3 only works if moto answers the same way. The last column points to the
test-side changes in section 2; the table doubles as the probe checklist for
a replacement.

| # | Surface | Silo (`RELEASE.2026-10-15T00-00-00Z`) | Real S3 | moto | Section 2 |
|---|---|---|---|---|---|
| G1 | PutBucketWebsite | `MalformedXML` on every put, checked before bucket existence; GetBucketWebsite `NoSuchWebsiteConfiguration`, DeleteBucketWebsite a no-op | supported; a missing bucket answers `NoSuchBucket` | supported; also accepts an empty configuration | T10 |
| G2 | `cp --sse` (SSE-S3, no KMS) | rejected, rc 1 | accepted, rc 0 | accepted | T11 |
| G3 | `--storage-class GLACIER` | `InvalidStorageClass`, rc 1 | accepted, rc 0 | accepted | T12 |
| G4 | Object annotations | directive header ignored, ListObjectAnnotations 500 | supported | not supported | T13 |
| G5 | CreateBucket on a bucket you own | `BucketAlreadyOwnedByYou`, rc 1 | us-east-1 succeeds; other regions `BucketAlreadyOwnedByYou` | us-east-1 behavior | T14 |
| G6 | `CreateBucketConfiguration.Tags` | accepted and ignored (empty tag set) | applied (*measured*) | ignored | T15 |
| G7 | Access points | none | supported | - | T16 |
| G8 | Keys with a `..` segment / `//` | PUT rejected (`XMinioInvalidResourceName` / `XMinioInvalidObjectName`) | accepted | - | T24 |
| G9 | Object `p` beside prefix `p/` (`pre` and `pre/x.txt`) | listing hides `pre/x.txt`; a purge misses it and it reappears in the next scenario's end state | two independent keys | - | T24 |
| G10 | Zero-byte PUT | the next request on the same pooled connection stalls until the read timeout | unaffected | unaffected | T1 |
| G11 | GetObjectAttributes, COMPOSITE checksum | value without the `-N` suffix (HeadObject has it); type only in `ChecksumType` | general purpose buckets answer in the same form; S3 Express directory buckets add the `-N` suffix (*measured*) | - | T25 |
| G12 | UploadPartCopy (multipart copy) | destination carries no checksum | destination carries a CRC64NVME FULL_OBJECT checksum (*measured*) | - | none |
| G13 | PutObject with a canned ACL or grant, new bucket | accepted, rc 0 | new buckets have ACLs disabled (BucketOwnerEnforced): rejected, rc 1 on both CLIs (*measured*) | accepted | T7 |
| G14 | CopyObject with `If-None-Match: *` onto an existing key | condition ignored, the copy overwrites (PutObject honors it) | honored: neither CLI copies (*measured*) | condition ignored | T9 |
| G15 | Bucket delete and re-create under one name | immediate | lags: re-create answers `BucketAlreadyOwnedByYou`, HeadBucket right after create can 404, right after delete can still find it (*measured*) | immediate | T6 |
| G16 | `LastModified` in listings | sub-second | whole seconds | - | none |
| G17 | Transport of the dev stack (a setup property, not server behavior) | http | https | in-process | T2, T8 |
| G18 | PutObject with an XXHASH64, XXHASH3, XXHASH128, or SHA512 checksum | `InvalidArgument` ("Invalid checksum provided."), rc 1; `pgsty/minio` `RELEASE.2026-08-04T00-00-00Z` accepted them | XXHASH64 accepted, rc 0 (*measured*); the others *unverified* | XXHASH64 accepted | T26 |

G16 changes no test - the e2e sync scenarios shift times by a day - but a
behavior that depends on it (`sync --exact-timestamps` converging) looks
different on the dev stack than on real S3. G17 matters because botocore
puts the flexible checksum into an aws-chunked trailer only over https, so
some failure paths differ by transport, and because aws's CRT client takes
`use_ssl` from the command line only.

## 2. Test-side changes

Every change in `tests/` and `scripts/` that exists because the endpoint is
not real S3, as of the 2026-10-15 inventory (T26 came with the switch to
Silo on 2026-10-17). "On a new endpoint" is what to do when the e2e endpoint
changes - to real S3 for a run, or to a replacement for the dev stack.
Changes to `src/` and `cli/src/` are product fixes, not test-side changes,
and are not listed.

| # | Change | Where | Gap | On a new endpoint |
|---|---|---|---|---|
| T1 | `Connection: close` on zero-byte directory-marker PUTs | `harness.seed_bucket` | G10 | drop on real S3 or moto (harmless if kept); probe a replacement for the stall |
| T2 | the CRT lane passes `--endpoint-url` to both CLIs when `AWS_ENDPOINT_URL_S3` is set | `test_crt_parity._crt_invocation` | G17 | needed while the endpoint is http; `run_e2e.sh` unsets the variable, so real S3 skips it |
| T3 | the dev stack reports us-east-1; bucket creation omits `LocationConstraint` there | `scripts/minio-env.sh` / `.cmd` (`AWS_REGION`), `harness.create_bucket_in_region` | - | the branch is real S3's rule and stays; check that a replacement reporting another region accepts `LocationConstraint` |
| T4 | `presign_region` has no fetch | `presign_scenarios.py` | none | the comment's premise ("MinIO rejects a scope signed for another region") no longer holds: Silo, like `pgsty/minio` before it, answers 200 for an ap-northeast-1 scope, so the fetch can be enabled on the dev stack already |
| T5 | sibling bucket names never end in `-an` | `e2e/conftest.py` (`_sibling_bucket`) | - | keep on every endpoint until account-regional BucketNamespace semantics are verified |
| T6 | one fixed sibling name (`-mb` / `-rb`) is deleted, re-created, and checked with HeadBucket at once, per scenario and between the two CLIs | `e2e/conftest.py`, `test_mb_parity._reset`, `test_rb_parity`, `harness.force_delete_bucket` / `create_bucket_in_region`, `run_e2e.sh` cleanup, [`testing.md`](./testing.md) section 3 | G15 | on real S3, give every scenario and side its own name (still not ending in `-an`) and update the cleanup; this caused 5 of the 11 real-S3 failures |
| T7 | `cp_upload_acl_public_read` / `cp_upload_grants_uri` goldens record rc 0 (also the `.windows` variants) | `cp_scenarios.py`, `goldens/cp/` | G13 | a real-S3 golden (rc 1) would not replay on moto (rc 0): make them `diff_only` |
| T8 | the aws-side stderr token for a directory uploaded without `--recursive` is `Is a directory` | `cp_upload_dir_no_recursive` / `_exclude` in `cp_scenarios.py`; the `[Errno 21]` comment of `mv_upload_dir_without_recursive` | G17 | over https aws fails with `Need to rewind the stream ...` instead (rc 1 on both sides, as before): token on `upload failed` only, or switch by transport |
| T9 | `cp_no_overwrite_copy_exists` / `mv_no_overwrite_copy_exists` goldens record the overwrite MinIO made | `cp_scenarios.py`, `mv_scenarios.py`, their goldens | G14 | a real-S3 golden (no copy) would not replay on moto (overwrites): make them `diff_only` |
| T10 | the 7 server-reaching website scenarios are `diff_only`; the success path is checked on moto; the e2e cleanup deletes any website configuration | `website_scenarios.py`, `test_website_golden.TestWebsiteOnMoto`, `test_website_parity.py` | G1 | all but `website_no_options` can carry goldens again on an endpoint that supports website (moto also accepts the empty configuration that real S3 rejects); keep the cleanup on real S3 |
| T11 | `cp_upload_sse` is `diff_only` | `cp_scenarios.py` | G2 | can carry a golden again |
| T12 | `--storage-class GLACIER` scenarios are `diff_only`; the glacier gate shapes run on moto | `cp_scenarios.py`, `TestCpGlacierOnMoto`, `TestMvGlacierOnMoto`, `TestSyncGlacierOnMoto` | G3 | the storage-class scenarios can carry goldens again; keep the gate shapes on moto (restores take hours on real S3) |
| T13 | `cp_copy_props_all_multipart` is live-only | `cp_scenarios.py` | G4 | stays live-only: moto has no annotations either |
| T14 | `mb_existing` is `diff_only`, and its stderr token on our side assumes the failure | `mb_scenarios.py` | G5 | `diff_only` stays (moto is us-east-1); in us-east-1 both CLIs succeed and the `make_bucket failed` token must go or depend on the region |
| T15 | the `mb_tags` golden records an empty tag set | `mb_scenarios.py`, `goldens/mb/mb_tags.json` | G6 | a real-S3 golden would not replay on moto: drop the tags from the golden and keep the aws-vs-ours comparison |
| T16 | no e2e for access points: `mv --validate-same-s3-paths` has none, `ls_accesspoint_arn` is `diff_only` | `mv_scenarios.py`, `ls_scenarios.py` | G7 | real S3 could host an access point for `mv` (needs permissions and a cost estimate); `ls_accesspoint_arn` stays `diff_only` (a foreign ARN fails differently per endpoint) |
| T17 | the presign replay deletes `AWS_SESSION_TOKEN` to match the token-less MinIO capture; the normalizer keeps `X-Amz-Security-Token` | `test_presign_golden.py`, `harness.normalize_presign_stdout` | - (credential type) | a real-S3 run with temporary credentials puts the token into aws's URL and fails every presign golden: mask the token in the normalizer, or require long-term keys |
| T18 | presign URLs are made path-style, the endpoint becomes `<ENDPOINT>`, a scope region equal to the default becomes `<REGION>` | `harness.normalize_presign_stdout` and helpers | - | needed on every endpoint (capture and replay differ in host form and region) |
| T19 | nonexistent buckets are named `<bucket>-no-such` (and similar suffixes) | `ls_scenarios.py`, `rm_scenarios.py`, `website_scenarios.py`, `cp_scenarios.py` | - | real S3 shares one namespace; the random bucket names of `run_e2e.sh` keep these absent, a fixed name might not |
| T20 | expected values justified by a MinIO observation: `ls_page_size_zero`, `rm_page_size_zero`, `cp_upload_no_guess_mime`, `cp_checksum_sha256` | the scenario files | - | none: all matched on real S3 (2026-10-15) |
| T21 | `cp_copy_props_none` probes only `Metadata`, calling the default `ContentType` endpoint-specific | `cp_scenarios.py` | - | Silo (like `pgsty/minio`) and moto both answer `binary/octet-stream`; check real S3, then probe `ContentType` too |
| T22 | `normalize_rm_stdout` sorts delete lines, justified by an observation on MinIO | `harness.py` | - | needed on every endpoint: the order comes from aws's parallel completion |
| T23 | the dev stack itself: image, keys, region, readiness wait, container name, the golden-regeneration hint, the benchmark guard that recognizes MinIO | `scripts/compose.dev.yaml`, `compose-up.sh`, `minio-env.sh` / `.cmd`, `golden.py`, `benchmarks/awsenv.py` and its tests | - | rewrite for the replacement |
| T24 | no scenario uses `..` / `//` keys or an object beside a same-named prefix (no live keys probe; the unit tier pins `..` bytes) | the scenario files | G8, G9 | keep the rule until a replacement is probed; no current seed or golden end state has either shape |
| T25 | a library unit test fakes the COMPOSITE answer in MinIO's bare form | `test_checksumcompare.TestCompositeForms` | G11 | keep: real S3 answers in both forms (G11), and the test pins both |
| T26 | `cp_checksum_xxhash64` is `diff_only` | `cp_scenarios.py` | G18 | can carry a golden again: real S3 and moto both accept XXHASH64 |

Not test-side changes, though they mention MinIO: the `diff_only` of the
negative page-size scenarios (the reason is moto, which raises `IndexError`
for `MaxKeys=-1`; MinIO and real S3 both answer `InvalidArgument`), the
`measured on MinIO` notes in `tests/lib/` (where a client-side behavior was
measured, not a gap), and the example endpoints in unit tests (never
contacted).

## 3. The dev stack's image and replacement candidates

Upstream `minio/minio` is archived, so the dev stack needs a maintained
endpoint. The candidates below come from a desk survey on 2026-10-06; of
them, only Silo has run the suite. Facts marked *checked* were confirmed
against the cited page or registry on the survey date; *source* means read
from the project's source tree at its HEAD that day, runtime behavior
unverified; everything else is unverified.

### The switch to Silo (2026-10-17)

`pgsty/minio`, the community fork the dev stack used, was renamed to
**`pgsty/silo`** on 2026-08-06 (<https://github.com/pgsty/silo>), and
`pgsty/minio:latest` stopped moving at `RELEASE.2026-08-04T00-00-00Z`. On
2026-10-17 `scripts/compose.dev.yaml` moved to `pgsty/silo:latest`
(`RELEASE.2026-10-15T00-00-00Z` that day). Only the image name changed: Silo
keeps the `server` command, the `MINIO_*` variables, and the ports, and
`pgsty/mc` still creates the buckets. The e2e suite passed 248 of 248 on
`pgsty/minio` and 247 on Silo; the one failure was G18, now T26.

Silo's compatibility audit (<https://silo.pgsty.com/compatibility/server/>)
lists deliberate divergences from upstream MinIO. Probed side by side with
`pgsty/minio` on 2026-10-17, two of them answer differently on paths this
suite could reach: Silo rejects the XXHASH and SHA512 checksums (G18), and
DeleteObject honors `If-Match` (412 on a mismatch, where `pgsty/minio`
deleted; neither CLI sends it, so no test changes). ListObjects on a missing
bucket answered `NoSuchBucket` on both images for the shapes probed.

### Candidates

| Candidate | Status (survey date) | For this suite |
|---|---|---|
| **pgsty/silo** (AGPL-3.0) | the dev stack since 2026-10-17; releases 2026-08 to 2026-10; effectively one maintainer | Drop-in (above): answers the section 1 rows as `pgsty/minio` did, plus G18. |
| **Zenko CloudServer** (`ghcr.io/scality/cloudserver`, Apache-2.0) | 9.4.7 on 2026-10-05 (*checked*); maintained by Scality; Docker Hub `zenko/cloudserver` stale since 2023 | `S3BACKEND=mem` keeps auth, data, metadata, and KMS in memory (*checked* in `lib/Config.js`), so a restart is a full reset. *Source*: website handlers, GetObjectAttributes, CRC64NVME default and trailing checksums, SSE-S3 via the in-memory KMS, extra storage classes via `VALID_STORAGE_CLASSES`. *Source*: no handling of `If-None-Match` / `If-Match` on PUT was found, which `--no-overwrite` relies on - probe first. |
| **Ceph RGW** (LGPL family) | Tentacle 20.2.4 / Squid 19.2.6 (2026-08-19) | The reference implementation s3-tests grew up with, but no light single container any more: `ceph/ceph-container` (the demo image) is archived; RGW without RADOS (`dbstore` / POSIX drivers) is marked experimental; SSE-S3 needs Vault. A candidate for an occasional reference run, not the everyday lane. |
| RustFS (Apache-2.0, CLA) | 1.0.0 GA 2026-09-16, 1.0.1 2026-10-03 (*checked*) | MinIO-like strictness: writes accept only STANDARD and REDUCED_REDUNDANCY, GLACIER is `InvalidStorageClass` (*checked* in `storageclass.rs`); `..` / `//` keys rejected and SSE-S3 needs a configured master key (*source*). A fallback if Silo stops. |
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

### Next step (proposed, not decided)

Stand up CloudServer (`S3BACKEND=mem`) beside Silo and probe every section 1
row plus conditional PUT and CopyObject, recording the results here.
