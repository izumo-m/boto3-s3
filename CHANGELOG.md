# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-v0.2.0...HEAD
[0.2.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-v0.1.0...boto3-s3-v0.2.0
[0.1.0]: https://github.com/izumo-m/boto3-s3/releases/tag/boto3-s3-v0.1.0
