# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

- The `[plugins]` section of the config file is no longer read: no `aws` plugin is loaded, and a section naming one is no error.
- `~/.aws/cli/alias` is no longer read: no alias applies, and a file there is no error; a shell alias does the same job.
- The interactive auto-prompt and the `autoprompt` extra are gone: `--cli-auto-prompt` and `--no-cli-auto-prompt` are accepted and ignored, and `AWS_CLI_AUTO_PROMPT` and `cli_auto_prompt` are not read.
- The config files are now read only when a subcommand runs: a broken config file or setting still stops every subcommand, but no longer `help` and `--version` (exit codes changed).
- Command-line errors are now reported in Python's argparse words (`Unknown options: ...` aside), and when several things are wrong, which one is reported may differ from `aws` (exit codes changed).
- Error reports are no longer reworded into `aws`'s text: S3's messages, Python's own wording and the missing-credentials and missing-region reports read as botocore and the host's Python write them, `AWS_CLI_OUTPUT_ENCODING` is only checked for an unknown codec, and a digit-led endpoint scheme is no longer refused on Python 3.10 (exit codes changed).
- `file://` and `fileb://` are now refused where `aws` mishandles them (a `fileb://` path argument, a `website` bucket read from a file, `--metadata fileb://`, a single `--grants` file), and a `--metadata` value no map can hold is reported in plainer words (exit codes changed).
- A transfer or delete whose connection is closed or times out before any response, and a multipart download or copy whose source object changed during the transfer (`PreconditionFailed`), now prints its failure line and fails the run, `rb --force` included (exit codes changed).
- A multipart copy whose post-copy tag write is answered `PreconditionFailed`, or whose tag write and cleanup delete both fail, now prints its failure line and fails the run, and `mv` keeps its source (exit codes changed).
- A download to stdout that stdout cannot take (full, or its reader gone) now prints its failure line and fails the run, and a command whose output cannot be written no longer ends at Python's 120 (exit codes changed).
- Under the CRT engine, a run with no credentials now fails each item with `Unable to locate credentials` and no traceback, a run with no region stops with botocore's missing-region report, and a FIPS pseudo-region signs for the plain region; `presign` names the expiry the CRT signer refuses (exit codes changed).
- A run cut short by Ctrl-C or a fatal error still prints the result and failure lines that come after it, a single `rm` whose delete fails during a Ctrl-C prints that failure, a local file the walk briefly cannot read is warned about instead of skipped in silence, and under the CRT engine a transfer cut short by Ctrl-C or a fatal error is no longer printed as failed (exit codes changed).
- A malformed `--grants` (on any route) is now refused before anything runs, an unreadable `@=` file in `--metadata` is now a command-line error (252, was 255), a refused run no longer leaves its destination directory behind, and `mb` / `rb` no longer report a created or removed bucket as failed when stdout cannot take the success line (exit codes changed).
- A path that names no bucket (`s3://`, `s3:///key`) is now refused before anything runs on every command but `ls`; a recursive `cp` / `mv` or a `sync` whose local source cannot be walked as a directory (a file, an unreadable directory, a symbolic link under `--no-follow-symlinks`) now stops before it starts instead of running on an empty source, which let `sync --delete` empty the destination; and an S3 timestamp that only `aws`'s local-time conversion cannot hold no longer stops a transfer or delete (exit codes changed).
- `retry_mode = legacy`, `api_versions`, `SSLKEYLOGFILE` and `BOTO_DISABLE_CRT` now take effect as for any boto3 application, and a too-old SDK's refusal of `--no-overwrite`, of `ls`'s bucket filters and of an S3 Express access point under `mv --validate-same-s3-paths` is reported as the library reports it (exit codes changed).

## [0.9.0] - 2026-10-18

- `--copy-props all` writes a multipart copy's oversized tags and its annotations in `aws`'s order, with `aws`'s requests only.
- The `[plugins]` section is now loaded like `aws` loads it: a plugin that cannot be imported or initialized stops every command, and a plugin's handlers on SDK events run.
- A `[toplevel]` `s3` entry in `~/.aws/cli/alias` now applies to every command, as it does to every `aws s3` one.
- `--sse aws:kms` now signs an MRAP, Outposts or S3 Express target with plain SigV4 like `aws`, `--no-sign-request` included (it used the endpoint's own signer, or sent the request unsigned).
- More `aws` parity in failure and interruption corners: a transfer or delete whose connection is closed or times out before any response prints nothing and does not fail the run where `aws` loses it too, `sync` carries on through an unsorted listing, `--delete` included, and `cp` / `mv --case-conflict` pair one against the destination the way `aws` does, a run cut short by Ctrl-C or a fatal error prints no result line after its first cancelled transfer, and a stdout or stderr that cannot be written no longer silences the other one's result lines (exit codes changed).
- Under the CRT engine, `rm` and `sync --delete` now delete on the CRT client like `aws` (still batched), so the CRT's retry policy and error text apply, a missing credential's included; a role's or SSO's credential that fails to refresh now fails each transfer and leaves `--dryrun` completing, as with `aws`, and the CRT engine runs on boto3 1.34 to 1.41 with the `crt` extra; on either engine a key S3 reports with a transient error, and every key of a batch request that fails as a whole, is now sent again on its own like `aws` instead of failing (a missing bucket's failure lines included; exit codes changed).
- A profile's `cli_auto_prompt` is now read like `aws` reads it: from the profile the environment selects, no longer from the one `--profile` names; and without the `autoprompt` extra, `on-partial` no longer runs a command twice.
- More `aws` parity at startup: `--version` with stdout closed no longer ends in a traceback, and runs carrying two mistakes at once (an unknown option beside a bad option value, an unusable instance-metadata setting, `retry_mode = legacy` beside a bad endpoint) now report the one `aws` reports (exit codes changed in those corners); a role's or SSO's credentials are now fetched as `aws` fetches them: in its region when `--source-region` or an alias's `--region` names another, and never under an empty CA bundle or a digit-led endpoint.
- On every host interpreter, an HTTPS proxy now receives aws's `Host` header for an IPv6-literal endpoint, and a report quoting Python's own text (an over-long number, a trailing comma in a `credential_process` answer) reads as `aws`'s.
- A download to stdout (`cp s3://… -`) whose body is cut and retried no longer writes its first bytes twice, and a CRT download whose file could not be put in place (a name too long, a directory at the destination) now fails instead of reporting success (where `mv` deleted the object).
- More `aws` parity in the subcommands: `rm` on an Outposts access point ARN closed with `:` no longer deletes the whole access point and its line keeps the `:`, unsupported ARNs and refused bucket names are reported as and when `aws` reports them, a `sync` or recursive `cp` destination typed with `..` after a symlinked directory is created where `aws` creates it, and a Ctrl-C now waits for a single `rm`'s delete and for a finished run's last result lines (exit codes changed in those corners).
- An S3 Express access point (`--xa-s3`) is now refused by `sync` and treated by `--case-conflict` as its directory bucket is, and `mv` warns about one and resolves it under `--validate-same-s3-paths`; `aws` does none of these, losing objects through `sync` and `mv` and skipping them under `--case-conflict` (exit codes changed). `presign` now signs one with S3 Express session credentials, like `aws`.
- On an SDK too old for them, `mb --tags` and `-an` bucket names, the newer `--checksum-algorithm` values and S3 Express directory buckets and access points are now refused up front with the version they need, and an explicit CRT engine whose `crt` extra brought a too-old awscrt now names the boto3 upgrade instead of the extra (exit codes changed).
- More `aws` parity on Windows and with S3-compatible endpoints: on Windows, `--exclude` / `--include` now match a `\` in a key as a separator and lower-case by Windows's rules, and a file whose name is all extension (`.json`) is uploaded untyped; a timestamp sent without a zone no longer stops `sync`, a download from an endpoint serving its ETag in another form (unquoted, weak, empty) no longer fails, and an unreadable `mime.types` fails each upload instead of the run (exit codes changed).

## [0.8.1] - 2026-09-07

- The default request checksum is now `CRC64NVME` like `aws` wherever the installed botocore can compute it; without awscrt, botocore's `CRC32` stays.
- An empty or blank CA bundle value (`--ca-bundle`, `AWS_CA_BUNDLE`, `REQUESTS_CA_BUNDLE`, a profile's `ca_bundle`) now fails the run like `aws` instead of silently turning TLS verification off.
- More `aws` parity on error paths and filter corners.

## [0.8.0] - 2026-08-13

- `~/.aws/cli/alias` is now read like aws: `[command s3]` entries run as subcommands, internal and external (`!`) alike.
- Assumed-role, web-identity and SSO credentials are now cached in `~/.aws/cli/cache` like aws, so repeated commands stop re-calling STS and an `mfa_serial` profile no longer prompts — or fails non-interactively — on every run.
- The credential chain now runs with the command's own settings like aws: `--region` / `AWS_REGION`, the `--cli-*-timeout` values and aws's retry defaults reach the STS and SSO calls it makes, and the cache entry it writes is aws's own form.
- Malformed S3 listing entries and timestamps the local zone cannot represent now stop `ls` and the transfer commands where aws stops, instead of being silently skipped (exit codes changed).
- A file or directory that changes underneath a recursive walk is now handled like aws — warn-skipped when it vanishes, descended when a file became a directory, re-read at its turn when rewritten — so the rest of the tree still transfers (exit codes changed in those corners).
- `--no-sign-request` now handles `--sse aws:kms` exactly like aws, engine split included: the classic engine signs and requires credentials instead of uploading anonymously, while the CRT engine stays anonymous (it previously signed).
- An out-of-order S3 listing stops `sync` with one `fatal error:` line instead of a traceback (aws keeps going there; see the differences guide), and `sync --delete` no longer reports spurious delete failures on a local destination aliased through symlinked directories.
- HTTPS proxies that require a standards-form CONNECT tunnel now work on every host interpreter, like aws.
- `SSLKEYLOGFILE`, `BOTO_DISABLE_CRT` and a configured `us_east_1_regional_endpoint` are now ignored the way aws ignores them, and a config carrying several mistakes reports the one aws reports.
- `ls` timestamps now match aws byte for byte, including zones whose UTC rules changed and years below 1000, and recursive listings keep the `PRE` lines aws prints.
- A broken or missing stdout is now reported the way aws reports it (exit codes changed for `mb`, `rb`, `rm`, streaming `cp` and piped listings).
- More parse-layer parity: a scheme-less `--endpoint-url` starting with a digit, an `--endpoint-url` or `--query` that cannot be parsed at all, and a broken `AWS_MAX_ATTEMPTS` beside a broken `AWS_RETRY_MODE` all now fail the way aws fails, without tracebacks.
- Local timestamps at `datetime`'s limits, the mtime a download stamps, and the guessed upload `Content-Type` now match aws on any host interpreter — exit codes and transfer decisions change in those corners.
- Two CRT-engine corners now match aws: Ctrl-C or a fatal error during a CRT transfer prints aws's per-item cancellation lines and exits 1 instead of dropping them (a mid-transfer Ctrl-C previously exited 0 silently), and an explicit `preferred_transfer_client = crt` fails construction-time errors even when another process holds the CRT slot instead of silently running the classic engine.
- A `multipart_chunksize` under 5 MB no longer multiparts uploads `aws` sends as a single request under the CRT engine.
- More aws parity in the transfer engine: `--metadata-directive COPY` keeps your properties on a multipart copy, annotation copies drop a checksum header `aws` omits, a failed annotation copy reports aws's failure text, and a bad option is reported ahead of an unusable source.
- Progress-line spacing and the displayed transfer speed now match aws where a transfer fails, a coarse clock reads no elapsed time yet, a notification omits an already-known size, or a case-conflict advisory prints — and the standing S3 Express warning flushes immediately, like aws's.
- A `--metadata k@=file://...` reference that cannot be loaded now reports and exits like aws (255, not 252), and the reports for an empty `fileb://` payload (`--metadata`, `website`) and for JSON metadata carrying non-string values now match aws's wording too.
- `AWS_CLI_OUTPUT_ENCODING` and the `cli_binary_format` config variable are now validated like aws: an unknown value stops the run at exit code 255. A valid codec is now also applied to error reports the way aws applies it, down to the exit code 255 when the codec cannot encode the report.
- Ctrl-C during a slow listing page pull now exits immediately like aws, whichever moment it lands in; previously some windows waited the in-flight request out first.
- `presign` URLs outside `us-east-1` now name the regional S3 host like `aws`, not the legacy global one.
- S3 Outposts access-point targets — every ARN spelling and full-length `--op-s3` aliases — and MRAP targets of any alias spelling now sign with SigV4a like `aws` (with the `crt` extra), instead of a plain SigV4 the endpoint rejects.
- `AWS_PROFILE` / `AWS_DEFAULT_PROFILE` no longer disable credentials passed in environment variables (aws binds only `--profile` that tightly), and a leftover `api_versions` config entry is ignored like aws instead of failing every command.
- S3's wrong-region and SigV4-migration errors now carry aws's extra guidance lines.
- `--metadata` now accepts aws's whole shorthand grammar (csv lists, nested hashes, explicit lists) and reports every rejection with aws's wording.
- More config-surface parity: an indented `cli_auto_prompt` block, an `[s3]` section carrying a `self` key, and a non-text `AWS_CLI_FILE_ENCODING` codec now fail the way aws fails.
- A single-file copy to the bare `s3:///` root names its destination key like aws, and a `HeadObject` 404 report keeps botocore's retry note.

## [0.7.0] - 2026-08-01

- `-h` / `--help` removed to match aws-cli — use `help`; they now fail with aws's `Unknown options` error. Help pages describe every option instead of listing bare names.
- Argument parsing restructured to aws-cli's shape: global options are consumed first wherever they sit (abbreviations, values interleaved with command options, `--` separators, options ahead of the subcommand, and `help` wrapped in globals now all parse like aws), `--exclude`/`--include` and `mb --tags` accept dash-led values like aws, tokens shaped like a negative number (`-1x`, `-2024-report.csv`) are accepted as option values and paths everywhere like aws instead of being rejected, and usage and error text now matches aws-cli's aside from the program name, including how aws reports errors when the named profile or a config file is broken and when credentials or a region cannot be resolved.
- More aws-cli parity on edge behavior: bucket-less `s3:///key` targets now fail at aws's stage with aws's wording (a dryrun upload/delete of one is rc 0), `--expected-size` is not converted under `--dryrun`, a broken `[s3]` config value beats the S3 Express `--case-conflict` rejection, an explicit `multipart_chunksize` under the CRT engine keeps aws's single-put/multipart cutoff, a named error code over HTTP 404 is no longer rewritten to the key-missing message, every subcommand now builds its S3 client at aws's stage and `rm` reads the `[s3]` config like aws, so exit codes changed when the region or the `[s3]` config is unusable, the `cli_timestamp_format` config variable is now read and validated like aws (both accepted values print the same as before), and an upload with no credentials or no resolvable region under the CRT engine now fails the way aws does instead of falling back to the classic engine.
- The CRT engine now verifies TLS against the same CA bundle as the classic engine and aws-cli, instead of the operating system's trust store.
- Fixed every command failing on a host where the IMDS address answers but is not EC2's metadata service (a non-EC2 cloud VM): region detection reads it as absent, like aws.

## [0.6.1] - 2026-07-23

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
- `rb --force` now resolves credentials once, sharing one session between the inner `rm` and the bucket delete like aws (a `credential_process` / MFA flow no longer prompts twice).

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

[Unreleased]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-cli-v0.9.0...HEAD
[0.9.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-cli-v0.8.1...boto3-s3-cli-v0.9.0
[0.8.1]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-cli-v0.8.0...boto3-s3-cli-v0.8.1
[0.8.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-cli-v0.7.0...boto3-s3-cli-v0.8.0
[0.7.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-cli-v0.6.1...boto3-s3-cli-v0.7.0
[0.6.1]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-cli-v0.6.0...boto3-s3-cli-v0.6.1
[0.6.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-cli-v0.5.0...boto3-s3-cli-v0.6.0
[0.5.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-cli-v0.4.0...boto3-s3-cli-v0.5.0
[0.4.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-cli-v0.3.0...boto3-s3-cli-v0.4.0
[0.3.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-cli-v0.2.0...boto3-s3-cli-v0.3.0
[0.2.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-cli-v0.1.1...boto3-s3-cli-v0.2.0
[0.1.1]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-cli-v0.1.0...boto3-s3-cli-v0.1.1
[0.1.0]: https://github.com/izumo-m/boto3-s3/releases/tag/boto3-s3-cli-v0.1.0
