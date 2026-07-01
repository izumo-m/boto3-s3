# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

- Progress repaints now have a 0.1 s floor when `--progress-frequency` is 0
  (the default): repaints run inline on the transfer worker threads, so
  unthrottled console I/O could serialize them and cap throughput on a slow
  terminal or pipe.
- Fix `--metadata` shorthand to accept an empty key like `aws` (`=bar` parses
  to `{"": "bar"}` and the transfer proceeds; it wrongly exited 252), and align
  the leading-comma error with aws's `Expected: '='` wording.
- Fix `sync` `--exclude` / `--include` anchoring to match `aws s3`: a
  root-anchored (absolute) pattern now prunes each side against its own full
  path, so a pattern matching only the source no longer wrongly protects the
  matching destination key from `--delete`.
- Fix the exit code for a non-integer `--cli-read-timeout` /
  `--cli-connect-timeout`: it now exits 255 (the value error reaches the general
  handler, as in `aws`) instead of 252 (an argument-parse error). A valid `0`
  still means "no timeout".
- Fix the `[s3]` transfer config to load from the same profile the client uses
  when both `AWS_PROFILE` and `AWS_DEFAULT_PROFILE` are set with no `--profile`
  (aws-cli precedence `AWS_PROFILE` > `AWS_DEFAULT_PROFILE`; stock boto3/botocore
  use the reverse, so the section could be read from the wrong profile).

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
