# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

- Add `S3.aws_config()`: read `~/.aws/config` with typed getters.
- Add `boto3_s3.etagfilter.ETagFilter`: opt-in ETag content-comparison filter for `sync`.
- Add `boto3_s3.checksumfilter.ChecksumFilter`: opt-in native-checksum
  (GetObjectAttributes) content-comparison filter for `sync` (awscrt-accelerated,
  pure-Python fallback).
- Add `GlobFilter`: fluent `exclude`/`include` builder for `cp`/`mv`/`rm`/`sync` `filter=`.
  `filter=` is now uniformly a `FileInfo` predicate (a raw `globsieve` matcher is no
  longer accepted directly); `FileInfo` gains `compare_key`.

## [0.1.0] - 2026-06-16

- Initial release.

[Unreleased]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-v0.1.0...HEAD
[0.1.0]: https://github.com/izumo-m/boto3-s3/releases/tag/boto3-s3-v0.1.0
