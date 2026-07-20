# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

- Settled the public API for 1.0: the sync-pair / result identity field is now `compare_key` (delete records join the same key space), `ls` takes `on_entry`, `S3Storage` speaks `uri`, unknown transfer options are rejected eagerly, capabilities became the sole custom-backend contract, and the export tiers shrank.
- Documented the frozen contracts (exception attributes, the two-tier export surface, the backend SPI evolution policy) and fixed a raw `ValueError` leaking from `client()` on a malformed endpoint.
- Aligned more behavior with aws-cli: an unset page size sends no `MaxKeys`, small multipart-copy tag sets stay inline on the create call, deferred annotation copies paginate, and CRT requests keep the caller's client configuration.
- Reused the installed default boto3 session for config reads, masked S3 Express session tokens, refused `--no-overwrite` uploads on too-old s3transfer, and fixed the module-level helpers' reported signatures.
- A fatal error now cancels accepted transfers like aws-cli instead of draining them; revoked items report the new `CANCELLED` outcome, with the `on_result` contract now documented.
- Swept sibling instances of past bug patterns: error translation and source-side attribution close remaining gaps, a pre-cancelled token no longer leaves side effects, and two shutdown/signal hangs are fixed.
- A missing awscrt where SigV4A signing is required (Multi-Region Access Points) now raises `ConfigurationError` instead of the base error.

## [0.7.0] - 2026-07-17

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

[Unreleased]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-v0.7.0...HEAD
[0.7.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-v0.6.0...boto3-s3-v0.7.0
[0.6.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-v0.5.0...boto3-s3-v0.6.0
[0.5.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-v0.4.0...boto3-s3-v0.5.0
[0.4.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-v0.3.0...boto3-s3-v0.4.0
[0.3.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-v0.2.0...boto3-s3-v0.3.0
[0.2.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-v0.1.0...boto3-s3-v0.2.0
[0.1.0]: https://github.com/izumo-m/boto3-s3/releases/tag/boto3-s3-v0.1.0
