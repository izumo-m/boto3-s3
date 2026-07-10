# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

- Recursive `rm` / `rb --force` and `sync --delete` now delete control-character
  keys like `aws s3` (via boto3-s3's per-key fallback).
- `--copy-props` gains the `all` choice, copying S3 object annotations
  (aws-cli 2.35.18 parity; needs botocore >= 1.43.31 and s3transfer >= 0.19).
- `file://` / `fileb://` paramfile references now expand in positional
  arguments and in the `mb` / `rb` / `presign` / `ls` / `rm` / `website`
  options, matching `aws s3`.
- An invalid `--query` expression now exits 252 like `aws s3` instead of being
  ignored.
- Closer `aws s3` parity in transfer progress output, shorthand syntax-error
  messages, `rb` error wording, and empty-`--profile` handling.
- The CRT transfer engine now honors an explicit `--endpoint-url` under an AWS
  domain (e.g. a VPC interface endpoint) instead of re-resolving to public S3.

## [0.4.0] - 2026-07-07

- Rebuilt on boto3-s3 0.5.0 (now the required floor).
- The "Unknown options" error now joins multiple names exactly like
  `aws s3` (`,` with no space - the s3 command layer's form).
- On Windows, `--exclude` / `--include` patterns written with `\` now match
  (via boto3-s3).
- A run-killing error inside the `cp` / `mv` / `sync` pipeline now always
  exits 1 with aws's `fatal error:` line, whatever the exception type (some
  previously escaped to the generic handler as 255).
- A symlink loop (or other unopenable directory) met during a local walk now
  warns and exits 2 like aws instead of silently succeeding (via boto3-s3).
- `--debug` now also masks signature-error hex byte dumps and credentials
  inside tracebacks (via boto3-s3).

## [0.3.0] - 2026-07-03

- Exit-code parity: the pre-pipeline validation order now matches `aws s3`'s
  measured parse-to-validation order (endpoint scheme -> paramfile loads ->
  integer coercions -> `--metadata` -> session profile -> path checks), so
  combined-error cases exit like aws - e.g. a bad `--profile` is 255 even
  alongside a usage error, and `cp`/`mv` `--recursive` pre-create the local
  destination directory (an uncreatable one is 255, not 1).
- `--metadata` shorthand accepts aws's `key@=file://...` paramfile operator,
  and paramfile expansion now covers the string-typed integer options
  (`--page-size` etc.), like aws.
- Clients now default to aws v2's retry behavior (`standard`, 3 attempts)
  when the env / profile config is silent; stock botocore's `legacy`/5 differed
  under throttling.
- Transfer result/progress rendering moved off the worker threads onto a
  dedicated printer thread (aws-cli's `ResultProcessor` shape): console I/O
  no longer throttles transfers. Deliberate deviation: the printer queue is
  bounded (10,000 records), so a long-stalled output consumer (e.g. `sync`
  piped into a stopped pager) back-pressures the run instead of growing
  memory without limit - aws-cli's queue is unbounded.
- Progress repaints now have a 0.1 s floor when `--progress-frequency` is 0
  (the default), keeping repaint records chunk-independent (display cadence
  only; aws repaints per chunk).
- Fix `--metadata` shorthand to accept an empty key like `aws` (`=bar` parses
  to `{"": "bar"}` and the transfer proceeds; it wrongly exited 252), and align
  the leading-comma error with aws's `Expected: '='` wording.
- Fix `mb` / `rb`'s bad-path error to match aws's bare form (no
  `usage: ... <S3Uri>` prefix - only `rm`'s error carries it).
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

[Unreleased]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-cli-v0.4.0...HEAD
[0.4.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-cli-v0.3.0...boto3-s3-cli-v0.4.0
[0.3.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-cli-v0.2.0...boto3-s3-cli-v0.3.0
[0.2.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-cli-v0.1.1...boto3-s3-cli-v0.2.0
[0.1.1]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-cli-v0.1.0...boto3-s3-cli-v0.1.1
[0.1.0]: https://github.com/izumo-m/boto3-s3/releases/tag/boto3-s3-cli-v0.1.0
