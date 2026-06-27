# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-06-27

- `--cli-auto-prompt`: offer option completion after a non-path-like second
  positional too (e.g. `cp s3://b/k outdir --<TAB>`), not just after a path-like
  one. The interactive prompt favors usability over `aws s3` parity.
- Rebuilt on boto3-s3 0.3.0.

## [0.1.1] - 2026-06-23

- Fix the CRT transfer engine on an old s3transfer: degrade to classic (or error
  clearly) instead of crashing. Rebuilt on boto3-s3 0.2.0.

## [0.1.0] - 2026-06-16

- Initial release.

[Unreleased]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-cli-v0.2.0...HEAD
[0.2.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-cli-v0.1.1...boto3-s3-cli-v0.2.0
[0.1.1]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-cli-v0.1.0...boto3-s3-cli-v0.1.1
[0.1.0]: https://github.com/izumo-m/boto3-s3/releases/tag/boto3-s3-cli-v0.1.0
