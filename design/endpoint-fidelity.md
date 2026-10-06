# Endpoint fidelity

Parity means boto3-s3 behaves like aws against real S3. Running the suite
against real S3 every time costs too much, so the e2e tier runs both CLIs
against a local substitute (MinIO) instead, with occasional real-AWS runs
([`testing.md`](./testing.md) section 3, endpoint policy). Wherever the
substitute answers differently from real S3, a pass on it is no evidence of
parity on real S3. This file records those places, the test-side changes made
because the endpoint is not real S3 (each must be revisited when the endpoint
is replaced), and the candidates for a replacement.

## 1. MinIO (pgsty/minio) against real S3

Every known way the dev stack's MinIO (`pgsty/minio`, `scripts/compose.dev.yaml`)
answers differently from real S3, in one place. The MinIO column is observed
behavior; the real-S3 column is S3's documented behavior, not a measurement
from this suite - no dated record of a full `tests/run_e2e.sh` run exists, so
a row marked *unverified* has never been checked against real S3 at all. The
last column is the change the suite carries for each gap - what to undo or
rework when the endpoint changes; the table doubles as the probe checklist for
a replacement.

| Surface | MinIO | Real S3 | Test-side change |
|---|---|---|---|
| PutBucketWebsite | `MalformedXML` on every put, checked before bucket existence; GetBucketWebsite `NoSuchWebsiteConfiguration`, DeleteBucketWebsite a no-op | supported; a missing bucket answers `NoSuchBucket` | server-reaching scenarios `diff_only`, success path on moto ([`testing.md`](./testing.md) section 7) |
| `cp --sse` (SSE-S3, no KMS) | rejected, rc 1 | accepted, rc 0 | `cp_upload_sse` `diff_only` |
| `--storage-class GLACIER` | `InvalidStorageClass`, rc 1 | accepted, rc 0 | `diff_only`; glacier gates on moto (`TestCpGlacierOnMoto`) |
| Object annotations | directive header ignored, ListObjectAnnotations 500 | supported | live-only `cp_copy_props_all_multipart` + library tests ([`testing.md`](./testing.md) section 7) |
| CreateBucket on a bucket you own | `BucketAlreadyOwnedByYou`, rc 1 | us-east-1 succeeds; other regions `BucketAlreadyOwnedByYou` | `mb_existing` `diff_only` |
| `CreateBucketConfiguration.Tags` | ignored (empty tag set) | applied - *unverified* here | `mb_tags` pins only aws-vs-ours no-divergence |
| Access points | none | supported | awscli port and unit tier |
| Keys with a `..` segment / `//` | PUT rejected (`XMinioInvalidResourceName` / `XMinioInvalidObjectName`) | accepted | no live keys probe; unit tier pins the bytes |
| Object `p` beside prefix `p/` (`pre` and `pre/x.txt`) | listing hides `pre/x.txt`; a purge misses it and it reappears in the next scenario's end state | two independent keys | avoid in scenarios; a lone re-run that turns MATCH means MinIO, not a regression |
| Zero-byte PUT | the next request on the same pooled connection stalls until the read timeout | unaffected | `Connection: close` on directory-marker PUTs (`harness.seed_bucket`); probes set a client timeout and seed zero-byte objects last |
| GetObjectAttributes, COMPOSITE checksum | value without the `-N` suffix (HeadObject has it); type only in `ChecksumType` | *unverified* | none |
| UploadPartCopy (multipart copy) | destination carries no checksum | *unverified* | none |

## 2. Replacement candidates (desk survey, 2026-10-06)

Upstream `minio/minio` is archived, so the dev stack needs a maintained
endpoint. This section records a desk survey of the candidates: nothing here
has been run against the suite yet. Facts marked *checked* were confirmed
against the cited page or registry on the survey date; *source* means read
from the project's source tree at its HEAD that day, runtime behavior
unverified; everything else is unverified.

### The current image is frozen

`pgsty/minio` was renamed to **`pgsty/silo`** on 2026-08-06
(<https://github.com/pgsty/silo>, *checked*). On Docker Hub `pgsty/minio:latest`
last moved on 2026-08-04 (`RELEASE.2026-08-04T00-00-00Z`) while
`pgsty/silo:latest` is at `RELEASE.2026-09-16T00-00-00Z` (*checked*), so the
`image: pgsty/minio:latest` in `scripts/compose.dev.yaml` receives no more
fixes. `pgsty/mc:latest` still moves (2026-09-16, *checked*). The compose
comment "CVE fixes only, no new features" no longer holds either: Silo's
compatibility audit (<https://silo.pgsty.com/compatibility/server/>, *checked*)
lists deliberate divergences from upstream MinIO, among them
`Server: Silo`, `NoSuchBucket` for ListObjects on a missing bucket (instead of
an empty listing on some paths), rejecting CRC64NVME + COMPOSITE, and
rejecting a presigned SigV4 request that declares
`STREAMING-UNSIGNED-PAYLOAD-TRAILER`. These move toward real S3, but goldens
captured on MinIO may need a re-check after the switch.

### Candidates

| Candidate | Status (survey date) | For this suite |
|---|---|---|
| **pgsty/silo** (AGPL-3.0) | releases 2026-08/09; effectively one maintainer | Drop-in: same single container, same tmpfs state, same `command:` per its Quick Start (unverified here). Expected to keep the section 1 gaps that are MinIO's own (unverified). |
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

### Next steps (proposed, not decided)

1. Switch the dev stack to `pgsty/silo` and re-run e2e and goldens to see what
   its divergences from MinIO change.
2. Stand up CloudServer (`S3BACKEND=mem`) beside it and probe every section 1
   row plus conditional PUT, recording the results here as section 3.
