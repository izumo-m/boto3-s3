# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

- Added aws-cli-compatible annotation preloading and fixed command-specific positional `fileb://`, dry-run validation, deletion, CRT endpoint, progress, and error-output parity.

## [0.4.0] - 2026-07-07

- Rebuilt on boto3-s3 0.5.0 and improved usage errors, Windows filtering, pipeline failures, symlink warnings, and debug-log masking.

## [0.3.0] - 2026-07-03

- Aligned validation ordering, shorthand parsing, runtime configuration, progress processing, transfer errors, and sync filtering with aws-cli.

## [0.2.0] - 2026-06-27

- Improved auto-prompt option completion and rebuilt on boto3-s3 0.3.0.

## [0.1.1] - 2026-06-23

- Made CRT selection degrade cleanly on older transfer dependencies and rebuilt on boto3-s3 0.2.0.

## [0.1.0] - 2026-06-16

- Initial release.

[Unreleased]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-cli-v0.4.0...HEAD
[0.4.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-cli-v0.3.0...boto3-s3-cli-v0.4.0
[0.3.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-cli-v0.2.0...boto3-s3-cli-v0.3.0
[0.2.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-cli-v0.1.1...boto3-s3-cli-v0.2.0
[0.1.1]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-cli-v0.1.0...boto3-s3-cli-v0.1.1
[0.1.0]: https://github.com/izumo-m/boto3-s3/releases/tag/boto3-s3-cli-v0.1.0
