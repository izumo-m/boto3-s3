# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

- Fix: `cp` / `mv` now apply `filter=` to a single S3 source too (an excluded
  object is neither transferred nor - the `mv` hazard - deleted), matching the
  recursive and `rm` paths.
- Masking now also covers the SSO bearer token (`x-amz-sso_bearer_token`) and
  the sso-oidc token bodies (`accessToken` / `refreshToken` / `idToken` /
  `clientSecret`) that botocore logs at DEBUG on the SSO auth path.
- Masking now also covers the SSE-C customer key in its boto3 parameter form
  (`'SSECustomerKey': ...` / `'CopySourceSSECustomerKey': ...`): s3transfer
  logs each task's kwargs at DEBUG with the raw key, which the wire-header
  patterns did not match - `set_stream_logger` / the CLI's `--debug` leaked it
  in cleartext.
- `LocalStorage.open` now anchors on the construction-time absolutized path
  like `scan` / `get_fileinfo`, so a later `chdir` cannot move where a relative
  location's keys resolve.
- `S3.mv` now supports a stream (`IOStorage`) **destination** for a single
  object - the bytes land on the stream, then the S3 source is deleted. A
  stream source (a move deletes its source, which a stream cannot be) and a
  recursive stream destination raise `ValidationError` with clear messages.
  The CLI keeps aws's blanket rejection of `-` for `mv`.
- A failed client build (e.g. `AWS_PROFILE` naming a missing profile) now raises
  the documented `ConfigurationError` from every public API - `S3.client()`, the
  lazy `S3Storage` default client, and the scan path previously leaked the raw
  botocore error.
- `S3.cp` / `mv` / `rm` / `sync` gain `capture_response=True`, which surfaces the
  operation's full S3 responses on `OpResult.extra_info`: the write response
  (`PutObject` / `CopyObject` / `CompleteMultipartUpload`, minus `ResponseMetadata`)
  under `"write"` with `"ETag"` promoted from it (so an upload carries one too), a
  download's `GetObject` response (`Body`-stripped) under `"read"`, and the removed
  object's `DeleteObject`-shaped response under `"delete"` (an `mv` source, or each
  `rm` / `sync --delete` object, the batched path reconstructed from the
  `DeleteObjects` batch). The `"write"` / `"read"` slots force the classic transfer
  engine (capture rides botocore client events the CRT data plane bypasses).
- `S3Storage.get_fileinfo(key)` now joins a non-empty child `key` under the
  prefix with a `/` boundary (was a bare concat that only resolved correctly
  when the prefix already ended in `/`), matching `LocalStorage` and the "an
  entry beneath it" contract.
- A root-anchored (absolute) `--exclude` / `--include` pattern now matches an
  entry's full key instead of a root-stripped one, so the single `sync` filter
  prunes each side against its own path (aws-cli's per-side roots).
  `globsieve.Matcher.included` gains a `full_key`, a new `Anchored` matcher joins
  each absolute pattern onto it, and `GlobFilter` passes `FileInfo.key`. Removed
  `globsieve.translate_pattern_for_root` and `TransferPlan.filter_root`.
- `--case-conflict` (`skip` / `warn` / `error`) now tracks only the downloads
  still in flight - the admitted key is dropped when its transfer finishes
  (aws-cli's `CaseConflictCleanupSubscriber`), not kept for the whole run - so a
  same-case twin is a conflict only while the first download is still running,
  matching aws on a case-sensitive filesystem.
- Spell the destination `dest`, not `dst`, throughout - matching aws-cli
  (`dest` is its spelling): `S3.cp` / `mv` / `sync`'s second argument,
  `ChecksumComparison`'s `dest`, `OpResult.dest_info` / `dest_storage`, and the
  internal types. A keyword caller switches `dst=` to `dest=`.
- `OpResult` now carries the operation's listing entries and backend handles -
  `src_info` / `dest_info` (`FileInfo`) and `src_storage` / `dest_storage`
  (`Storage`), so a consumer can act on a result object directly (e.g.
  `src_storage` + `src_info.key` to HeadObject it) - plus `extra_info`, the
  result's S3 response metadata (`{"ETag": ...}` for an s3-to-s3 copy and a
  download's source; an upload leaves it `None`, as s3transfer discards the
  PutObject response). `error` is now typed `Boto3S3Error` (was `BaseException`).
  See docs/opresult.md for which operation populates which field.
- Rename `OpKind` to `TransferType`, and the `kind` field on `OpResult` /
  `TransferProgress` to `transfer_type` (aws-cli's name for the record's verb).
  Frees `.kind` to mean only a `FileInfo`'s `FileKind`.
- `S3Deleter.submit` now takes the `FileInfo` to delete (was a `str` key): the
  deleter buffers listing entries by `info.key`, so a richer subtype
  (`S3FileInfo` with its `etag`) rides through to each `OpResult`. Callers pass
  `info` rather than `info.key`.

## [0.3.0] - 2026-06-27

- `Storage.delete` now takes the `FileInfo` to remove (was a `str` key): a
  backend deletes exactly the entry `scan` / `get_fileinfo` produced, by
  `info.key` in its own address space, and `cp` / `mv` / `sync` / `rm` route
  every delete through it (local ones included - no more bare `os.remove`). A
  custom backend's `delete` signature changes to match.
- `walk_local` is now a `LocalStorage` method (was a module-level function). The
  recursive local walk is split into protected, overridable methods (`_walk` /
  `_should_ignore` / `_triggers_warning` / `_stat_info` / `_stat_one`), so a
  subclass can extend the traversal - e.g. to follow Cygwin `!<symlink>` files on
  a native-Python Windows build - without re-implementing it.
- Add `detect_symlink_loops` to `cp` / `mv` / `sync` (and `ScanOptions`), default
  `False`: a recursive local walk skips a directory that resolves to one of its
  own ancestors with a `Symbolic link loop detected` warning, instead of
  recursing until `RecursionError`. A library extension - `aws s3` has none, so
  off keeps parity and costs no extra `stat`. The ancestor-stack guard is exposed
  as the reusable `boto3_s3.localstorage.LoopDetector` for custom walks.
- `cp` / `mv` / `sync` now transfer a custom `Storage` backend as one side (the
  other side S3), moving its bytes through `Storage.open()`: a custom source
  uploads to S3, an S3 source downloads into the backend, `mv` deletes a custom
  source via `Storage.delete()`, and `sync` works when the custom side declares
  `SORTED_SCAN` (its merge-join needs byte-ordered listings). The custom side is
  capability-checked up front.
- Add `ScanOptions.sort`: request byte-ordered enumeration (set by `sync`, whose
  merge-join needs it; `cp` / `mv` / `ls` / `rm` leave it off). A `SORTED_SCAN`
  backend honors it; the built-ins always sort and ignore it.
- Add `Storage.get_fileinfo(key="")`: the single-entry counterpart to `scan`
  (returns a `FileInfo`, or `None` if absent). `cp` / `mv` resolve a single
  source through it; a `Storage` subclass must now implement it.
- `Storage.scan` now stamps `FileInfo.compare_key` (the scan-root-relative key)
  on every entry, so a custom `ScanOptions.filter` predicate can match it directly
  instead of stripping `key`.
- Add `Storage.as_text()` (and `str(Storage)`): a Storage's canonical `aws s3`
  path-shape token (the inverse of `S3.resolve`).
- `cp` / `mv` / `sync` reject a non-built-in `Storage` (a custom backend, or a
  stream on a non-stream route) with a clear `ValidationError` (was an
  `AssertionError`).
- `S3Storage` construction is now permissive (parse only); the strict aws-cli
  rejections (S3 Object Lambda / Outposts bucket ARNs, a key with no bucket) move
  to `S3Storage.validate()`, which the operations run before use. Add the
  `Storage.scheme` discriminator (`"s3"` / `"local"`, or a non-built-in backend's
  own token); its type is an open `str` so a custom backend can declare its own.
- Add `Storage.capabilities` (a `StorageCapability` flag set: `OPEN_READ` /
  `OPEN_WRITE` / `GET_FILEINFO` / `SCAN` / `SORTED_SCAN` / `DELETE`) declaring
  which transfer operations a backend implements, so a transfer can pre-check a
  custom side instead of failing deep. `StorageCapability` is exported from
  `boto3_s3`.

## [0.2.0] - 2026-06-23

- Add streaming `cp`: wrap a stream in `IOStorage` / `StdioStorage` and pass it
  as a `cp` side (e.g. `cp("s3://b/k", IOStorage(buf))`); `cp` accepts any
  `Storage`, transferring a stream through its `open()`.
- Add `S3.aws_config()`: read `~/.aws/config` with typed getters.
- Add `sync(compare=...)`: a single copy-decision axis - the size+mtime default
  is `AwsCliComparison()` (tuned via `AwsCliComparison(size_only=...)` /
  `(exact_timestamps=...)`), or a content strategy.
- Add content `compare=` strategies `boto3_s3.etagcompare.EtagComparison` and
  `boto3_s3.checksumcompare.ChecksumComparison` (native checksum via GetObjectAttributes,
  awscrt-accelerated with a pure-Python fallback).
- Add `ParallelCompare`: run a content `compare=` strategy on a thread pool in `sync`.
- Add `GlobFilter`: fluent `exclude`/`include` builder for `cp`/`mv`/`rm`/`sync` `filter=`.
  `filter=` is now uniformly a `FileInfo` predicate (a raw `globsieve` matcher is no
  longer accepted directly); `FileInfo` gains `compare_key`.
- Speed up mixed-shape `exclude`/`include` lists: `globsieve` partitions them by
  shape into a folded `CompositeSet` instead of one big regex.
- Fix a recursive-download parent-directory escape (keys like `data//../secret`);
  path-traversal hardening, matching aws-cli.
- Fix crashes on the SDK floor: the transfer config and the CRT engine now
  degrade cleanly on an old s3transfer / boto3 instead of raising.

## [0.1.0] - 2026-06-16

- Initial release.

[Unreleased]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-v0.3.0...HEAD
[0.3.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-v0.2.0...boto3-s3-v0.3.0
[0.2.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-v0.1.0...boto3-s3-v0.2.0
[0.1.0]: https://github.com/izumo-m/boto3-s3/releases/tag/boto3-s3-v0.1.0
