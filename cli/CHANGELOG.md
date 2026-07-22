# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

- Large listings (`ls` / `sync` / `rm` over many objects) got severalfold faster: response timestamps now parse at C speed.
- Aligned shorthand trailing-whitespace parsing, legacy retry-mode rejection, paramfile text encoding, and the unset `--page-size` wire shape with aws-cli.
- Matched aws-cli's deeper classic-download IO queue (1000 buffered chunks where boto3 defaults to 100).
- Matched aws-cli's error attribution when a global option fails to parse: it now beats an invalid subcommand and `-h`, and `--version` beats both.
- A fatal error mid-run now stops the queued transfers like aws-cli (previously they all completed before the fatal exit).
- Matched aws-cli's `--exclude`/`--include` evaluation exactly: patterns are joined onto both sides' paths, so glob characters in the operation path, nested s3-to-s3 paths, and single-object keys with unusual shapes (doubled slashes) now filter like aws.
- More aws-cli parity on option edge cases (`--metadata` shorthand csv quirks, `--expected-size` typing, website/rm paramfile forms), and console output now survives unencodable keys like aws.
- Multi-Region Access Point targets now sign with SigV4A like aws (with the `crt` extra; without it they fail with a clear configuration error instead of an invalid signature).
- Ctrl-C during a running transfer or delete now exits 1 with aws's `cancelled: ctrl-c received` line (previously 130; outside the run 130 stays).
- S3 Express directory bucket targets now sign with their session-based scheme like aws (previously the always-SigV4 pin produced invalid signatures, visibly in `presign` URLs).
- The CRT engine now honors an explicit `--endpoint-url` under an AWS domain (a VPC interface endpoint) instead of re-resolving to public S3.

## [0.6.0] - 2026-07-17

- Aligned global-option ordering, help tokens, Ctrl-C, and region fallback with aws-cli, changing some exit codes to match; made auto-prompt completion more responsive.

## [0.5.0] - 2026-07-12

- Added annotation preloading and improved command behavior parity with aws-cli.

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

[Unreleased]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-cli-v0.6.0...HEAD
[0.6.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-cli-v0.5.0...boto3-s3-cli-v0.6.0
[0.5.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-cli-v0.4.0...boto3-s3-cli-v0.5.0
[0.4.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-cli-v0.3.0...boto3-s3-cli-v0.4.0
[0.3.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-cli-v0.2.0...boto3-s3-cli-v0.3.0
[0.2.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-cli-v0.1.1...boto3-s3-cli-v0.2.0
[0.1.1]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-cli-v0.1.0...boto3-s3-cli-v0.1.1
[0.1.0]: https://github.com/izumo-m/boto3-s3/releases/tag/boto3-s3-cli-v0.1.0
