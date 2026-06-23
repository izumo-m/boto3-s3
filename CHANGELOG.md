# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

## [0.1.0] - 2026-06-16

- Initial release.

[Unreleased]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-v0.1.0...HEAD
[0.1.0]: https://github.com/izumo-m/boto3-s3/releases/tag/boto3-s3-v0.1.0
