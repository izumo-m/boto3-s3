# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

- `SyncPair` now always carries both sides (`src` / `dest` non-optional); one-sided sync pairs became the new `SrcOnlyPair` / `DestOnlyPair` types (`MergedPair` union).
- Improved cancellation robustness, local path-limit parity, and secret masking coverage; added a storage option to exit scans immediately on interrupts.
- Sped up CRT transfer startup by reusing the caller's session for request serialization.

## [0.6.0] - 2026-07-12

- Expanded local scanning and transfer controls while improving operation reliability and aws-cli parity.

## [0.5.0] - 2026-07-07

- Deepened the `Storage` scan contract, sync decision filters, response capture, content comparison, and local filesystem customization.

## [0.4.0] - 2026-07-03

- Improved transfer correctness, secret masking, custom backend behavior, SDK-floor degradation, and path/filter parity.

## [0.3.0] - 2026-06-27

- Added the capability-based `Storage` extension model, custom-backend transfers, configurable local walking, and symlink-loop detection.

## [0.2.0] - 2026-06-23

- Added streaming copies, AWS config access, sync comparison strategies, parallel comparison, glob filtering, and old-SDK compatibility.

## [0.1.0] - 2026-06-16

- Initial release.

[Unreleased]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-v0.6.0...HEAD
[0.6.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-v0.5.0...boto3-s3-v0.6.0
[0.5.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-v0.4.0...boto3-s3-v0.5.0
[0.4.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-v0.3.0...boto3-s3-v0.4.0
[0.3.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-v0.2.0...boto3-s3-v0.3.0
[0.2.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-v0.1.0...boto3-s3-v0.2.0
[0.1.0]: https://github.com/izumo-m/boto3-s3/releases/tag/boto3-s3-v0.1.0
