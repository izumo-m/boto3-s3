# boto3-s3-cli design (the CLI layer)

`boto3-s3-cli` is the `aws s3`-compatible CLI distribution that sits on top of
the `boto3-s3` library, a by-product of the library, which is the product.
Functional compatibility with `aws s3` is the library's responsibility; this
layer narrows the inputs to exactly `aws s3`'s (strict argument validation,
the option system) and owns the output and the exit codes. For the
responsibility split, see [`overview.md`](./overview.md) sections 1 and 3; for
the handling of individual options, see
[`aws-cli-option-handling.md`](./aws-cli-option-handling.md).

This document records the solidified design for the implemented scope (currently
`cp` / `ls` / `mv` / `rm` / `mb` / `rb` / `presign` / `sync` / `website`). Only
solidified design is added here.

## 1. Entry point and dispatch

- The console script `boto3-s3` -> `boto3_s3_cli.cli:main`.
- `boto3-s3` is the top-level command equivalent to `aws s3`. Subcommands (`cp` /
  `ls` ...) are registered as argparse subparsers.
- `main(argv, *, ctx=None)` parses -> dispatches to the corresponding
  subcommand's `Command` instance -> returns the exit code (an int). argparse's
  `SystemExit` is also absorbed inside `main` and converted into an exit code, so
  `main` always returns an int - with two deliberate escapes: an
  `AssertionError` is re-raised (a broken invariant should crash loudly, not
  fold into an rc; section 6), and a command-raised `SystemExit` is honored.
  The exit codes for exceptions and usage errors are covered in section 6.
- Nothing before the command reads aws's config or credentials files. aws
  builds its botocore session before it parses the command line, so a broken
  file or setting fails even its `help` and `--version`; here botocore reads
  the files when a command opens its session (section 6), so the
  informational exits succeed under a broken config and stay SDK-free
  ([`imports.md`](./imports.md)). This is the accepted ordering difference of
  [`overview.md`](./overview.md) section 3: every command that does anything
  still stops, with an exit code of the same meaning. The config file's
  `[plugins]` section, which aws loads at the same point, is not read
  ([`aws-differences.md`](../docs/cli/aws-differences.md)).
- aws reads its **alias file** (`~/.aws/cli/alias`) next, while it is still
  assembling its parser; this CLI does not read it
  ([`aws-differences.md`](../docs/cli/aws-differences.md)).
- Then comes the aws-shaped **top-level globals pass**: a
  globals-only `parse_known_args` over the full argv (aws's `MainArgParser`)
  that *consumes* the globals - its remainder is the only token stream the
  later stages see, exactly as aws's command layers only see what
  `MainArgParser` left over. Consuming here is what makes the measured aws
  behaviors fall out: a global is recognized on either side of the
  subcommand and even between a command option and its value (`presign
  --expires-in --region us-east-1 120` is rc 0), abbreviation resolves
  against the globals alone (`--e` is `--endpoint-url`, never ambiguous with
  `--expires-in`), and a leftover option-form token ahead of the subcommand
  flows down to the leaf parser (`--expires-in=120 presign s3://b/k` is
  rc 0). The parse itself settles two outcomes on the spot, written by the
  parser as it exits: a global that fails to parse (invalid choice, missing
  value) is a usage error (252), and a parse-time `--version` prints and
  exits 0. Help is
  reached only through aws's **help-token rule**: an exactly-`help`
  post-globals remainder, or an exactly-`help` stage-2 token list after a
  subcommand, prints the corresponding help page at rc 0 (`ls help` shows
  ls's help rather than listing a bucket named `help`; `help foo` stays the
  invalid-choice 252, like aws; `presign help --region us-east-1` still pages
  because the globals are already gone, like aws). No parser declares an
  argparse help action, so `-h` / `--help` are unrecognized options exactly
  as on aws (`s3 --help` and `s3 ls -h` are `Unknown options: ...`;
  `s3 ls --h` still abbreviates to `--human-readable`; and
  `s3 help --help` breaks the exactly-`help` remainder into an invalid
  choice `help` - all measured on 2.36.40).
- The first `--` stops the globals pass (argparse semantics, verified
  identical on 3.10 and aws's bundled 3.14) and the marker survives in the
  remainder for stage 2's parse to honor, so the tail stays positional all
  the way down (`presign -- s3://b/k --expires-in 120` is aws's
  `Unknown options: --expires-in,120`, measured). The one exception is a
  *leading* `--`: aws's first parse uses it up against the always-preceding
  service token (`s3 -- help` pages; `s3 -- presign --expires-in 120
  s3://b/k` re-reads the option at the leaf, rc 0 - both measured), so ours
  drops the marker off a `--`-led argv after the pass.
- `ctx` (`Context`) is the injection point for runtime dependencies (section 3.1). When
  not supplied, the real one (the default `Context()`) is assembled. Tests pass a
  `Context` loaded with fakes.
- **Text encodings, first of all.** The console script calls `main()` with no
  `argv`, and before anything else `main` sets the process up the way aws's
  encoding decision would (`textencoding`; design/overview.md section 3,
  "Text encodings"): standard output and standard error are re-encoded where
  Python chose another encoding (UTF-8 mode - Python 3.15's default, or
  `PYTHONUTF8=1` - or `PYTHONIOENCODING`), stdout with the error handler
  aws's interpreter gives it there (`strict` - so `output.uni_write` prints
  `?` as aws's `uni_print` does - except `surrogateescape` on Windows and in
  the C / POSIX locales) and stderr with `backslashreplace`; a Windows
  console is left alone, it takes Unicode. Then `sys.argv` is read again
  with the CLI's filename encoding where it differs from the file system
  encoding (Linux and the other non-Apple POSIX systems): each argument's
  bytes (`os.fsencode`) decoded with it, `surrogateescape` for bytes it
  cannot decode, where aws's bootloader refuses them (rc 255). An `argv`
  given by a caller (the tests, an embedding application) is taken as it is
  and the streams are left alone, except that an `[ERROR]` report re-encodes
  standard error under `AWS_CLI_OUTPUT_ENCODING` or `PYTHONUTF8=1`, as aws's
  does. Every local path the CLI builds a
  `LocalStorage` for, or probes itself (the missing-source and
  destination-directory checks, `file://`, `--ca-bundle`), is that text, handed to the OS in
  its own form (`textencoding.os_path`); the `file://` encoding falls back to
  the same text encoding when `AWS_CLI_FILE_ENCODING` is unset. The config and
  credentials files are botocore's to read and are not converted.

## 2. Parser construction (standard argparse, no added dependencies)

- The top-level globals pass owns every global (its namespace, defaults
  included, seeds the stage-2 parse), so placing one before or after the
  subcommand is the same parse (both `boto3-s3 --profile foo ls s3://b` and
  `boto3-s3 ls s3://b --profile foo` work, matching `aws s3 --profile foo ls
  ...`). The **rendering** parsers - each subcommand's help page and the
  `build_parser()` tree - still take the
  `suppress_defaults=True` globals parent so their help keeps listing the
  globals; the parser stage 2 actually parses with
  (`_build_command_parse_parser`) knows only the command's own arguments,
  like aws's leaf parser. Whatever that parse cannot place is reported right
  after it as `Unknown options` (252).
- The subcommand is located by `_find_command_token` - aws's
  `SubCommandArgParser` shape: unknown optionals are assumed valueless, the
  first positional-looking token (argparse's classification: not dash-led, a
  lone `-`, a negative-number opening, a token with a space, or anything
  behind `--`) names the command, and only that token leaves the stream. It
  shares the pinned matcher below with the parsers, so both stages classify a
  token identically (`s3 -1x` is aws's invalid choice `'-1x'`, not an unknown
  option - measured). A name that is not in `_COMMAND_TABLE` is an invalid
  choice (252), reported as aws's command-table positional reports it
  (`argument subcommand: Found invalid choice '<name>'` - argparse names a
  positional after its dest, so the report says a bare `subcommand` where the
  help page displays `<subcommand>`), and an option-form token *can* be that
  name once it sits behind a `--` of its own (`--region us-east-1 -- --bogus`
  is aws's invalid choice `'--bogus'`, measured). With no command token at
  all, surviving option-like tokens are reported as `Unknown options` (aws:
  `s3 --bogus`, and a leading `--` is used up by the globals pass, so
  `s3 -- --profile` is one too), and with nothing left at all it is aws's
  bare `too few arguments` usage error (`_TOO_FEW_ARGUMENTS`). No command
  module is imported on any of those paths. Each subcommand class
  adds its own arguments via `configure(parser)` on the real stage-2 parser.
- **Parse error text.** aws builds every parser on one base class and hands
  its main, service and leaf parsers the same usage string, so *every*
  argparse failure folds `<message>\n\n<usage>` into a single `[ERROR]`
  report, enveloped as a usage error (252, section 6).
  `_ParamValidationArgumentParser` is that base here, and its reports are
  aws's under one mapping: `aws [options] s3` -> `boto3-s3 [options]`, with
  aws's `<command> <subcommand>` hierarchy collapsed onto our single level
  (this command *is* `aws s3`), which also drops the **first** line of its
  help blurb - `aws help`, the level above us; its `aws <command> help` is
  our `boto3-s3 help` and its `aws <command> <subcommand> help` our
  `boto3-s3 <subcommand> help`. The globals-parse and subcommand parse
  failures, and a rejected subcommand name, close with that one block
  (`_TOP_LEVEL_USAGE`); the `Unknown options: <a>,<b>` reports (joined with
  a comma and no space) - raised by aws's command layers, not a parser -
  carry no usage. Off-list values are reported by `_check_value`, aws's hook:
  `Found invalid choice '<value>'` plus the close matches of the choice list
  (difflib, cutoff 0.8 - aws's, so `lss` offers `ls` while `web` offers
  nothing). The rest of a report is argparse's own template, filled from
  declarations that follow aws's: a missing positional is named by its dest
  (`paths`, `path`), and an option declared with a token count (`aws_nargs`)
  reports a missing value as `expected 1 argument`. A `--profile` or
  `--debug` failure closes with the usage of aws's first-pass parser
  (`usage: boto3-s3 [--profile PROFILE] [--debug]`), which reads those two
  ahead of the rest. One interpreter difference remains that decides
  what is accepted, not how a failure reads:
  - **Ambiguous abbreviations met before their turn** (Python 3.10 / 3.11
    only). Argparse raises the ambiguity there while *classifying* the token
    stream, a pass that runs to completion before anything is consumed; 3.12
    moved the check to the point of use. So below 3.12 a value that
    ambiguously abbreviates one of the command's own options - `--exclude
    --ss` against cp's `--sse*` family - is rejected where aws takes it as
    the value. Closing it would mean overriding the classification too
    (`_parse_optional` / `_get_option_tuples`), far more private argparse
    surface than a corner input is worth.
- **Counted option values.** aws declares `--exclude` / `--include` with
  `nargs=1` and `mb`'s `--tags` with `nargs=2`, which lets those tokens be
  taken however they look (`--exclude '-foo*'`,
  `--exclude --include`, `mb --tags -k -v`). That is not aws's
  code but its interpreter: stock argparse gives a count-declared option
  option-like tokens only from Python 3.12 on (its nargs pattern became
  `[AO]{N}`), and aws-cli, source-installed across 3.9 to 3.14, flips at
  exactly that boundary - the shipped distribution bundles 3.14, so consuming
  is what its users get. Each such option's action carries the count as
  `aws_nargs` (`AppendFilterAction`, mb's `_AppendTagsAction`) and
  `_ParamValidationArgumentParser` reads the marker in a `_match_argument`
  override that applies the 3.12+ rule on every supported Python. The consumption is visible beyond the option itself,
  since a swallowed token no longer counts as a positional:
  `cp --exclude --include x s3://a/ s3://b/` is aws's
  `Unknown options: s3://b/` (three paths, one too many) and
  `sync --exclude -h a b` its local-to-local synopsis error, both reproduced.
  Only the count is decided there; what class a token belongs to stays
  argparse's own decision, which is why `--` never becomes a value
  (`--exclude -- a b` is the missing value, as on aws) and why the
  ambiguous-abbreviation residual above survives on 3.10 / 3.11.
  `--grants` (`nargs='+'`) needs no marker: its pattern is version-stable.
- **Missing standard streams.** A descriptor closed before startup leaves
  `sys.stdout` `None`. `--version` then writes its line to stderr and exits
  0, as argparse's own version action does, and a help page falls back to
  stderr the same way (aws writes it nowhere). Both streams closed is the
  host interpreter's; a write that fails ends as section 6 describes (the
  final flush).
- **Negative-number classification.** The other place aws's behaviour comes
  from its interpreter rather than its code. argparse asks
  `_negative_number_matcher` whether a dash-led token that matches no option
  string is a plain value or an option, and Python 3.14 loosened the pattern
  from a whole-token number (`^-\d+$|^-\d*\.\d+$`) to a prefix (`-\.?\d`,
  applied with `.match`), so `-1x` and `-.5x` became plain values. Since the
  shipped distribution bundles 3.14, `cp --storage-class -1x s3://a/k
  s3://b/k` is aws's invalid choice `-1x` and
  `ls --page-size -1x s3://a/` its `invalid literal for int()` at rc 255,
  where the older pattern would report a missing value.
  `_ParamValidationArgumentParser` therefore pins 3.14's pattern as a
  property (argparse's `__init__` assigns the host Python's over any plain
  attribute), which covers every parser the dispatch builds - the globals
  pass as much as a leaf - just as aws's one interpreter
  covers its whole `CLIArgParser` family. `_find_command_token` reads the
  same constant, so the subcommand scan and the parsers never disagree.
  The effect is not confined to error text: a dash-led value or path shaped
  like a negative number is now *accepted* where it used to be rejected
  (`cp --dryrun -2024-report.csv s3://b/k` uploads, matching aws).
  The boundary is unchanged for the marked options above, which consume a
  token whatever its class, and for `-x` / `-.x` / `-.` / `--1`, which stay
  option-like on aws too. One half of argparse's rule is left to the host:
  the gate that disables the classification once a registered option string
  itself looks like a negative number is evaluated per container, and the
  optionals live on plain argument groups. No option string here is
  digit-led, so the gate is empty everywhere; a test pins that, since a
  digit-led option would need the gate pinned too.
- **The MIME table an upload's `ContentType` is guessed from** is pinned the
  same way, one layer down: the library carries the shipped distribution's
  3.14 `mimetypes` tables rather than reading the running interpreter's, which
  change between releases ([`transfer.md`](./transfer.md) section 1).
- **The CONNECT request an HTTPS proxy sees** is pinned the same way, one
  layer further out: not a report's wording but a request on the wire. CPython
  3.12 rewrote the tunnel opener - `CONNECT host:port HTTP/1.1` carrying the
  `Host` header HTTP/1.1 owes its recipient, where 3.10 and 3.11 write
  `HTTP/1.0` and no `Host` (a shape urllib3 carries its own copy of, so a
  newer urllib3 does not move it) - so a proxy that answers 400 to a
  `Host`-less CONNECT served every `aws` command and failed every command
  here. `proxytunnel.pin_connect_request` installs the 3.12+ opener onto
  urllib3's `HTTPConnection` from `clientfactory._create_client`, the one seam
  every client this CLI hands out passes through, which puts one CONNECT on
  the wire across every supported Python.
- **Interpreter wordings are the host's.** Where a report quotes text the
  interpreter wrote - a `json` decode failure, `int()`'s digit limit, a
  codec's `LookupError`, the status phrase botocore puts in a body-less S3
  error answer - it reads as the running Python writes it, not as aws's
  bundled 3.14 does ([`overview.md`](./overview.md) section 3). The same
  goes for which URL schemes `urlsplit` accepts: on 3.10 a digit-led scheme
  parses, so `--endpoint-url 192.168.0.9:9000` reaches botocore's `Invalid
  endpoint:` at rc 255 where aws rejects it at parse time (252), and an
  endpoint from the environment or the config such as `1http://...` is not
  refused when the client is built (aws: 255); each request then fails
  against it.

## 3. Module layout

| module | role |
|---|---|
| `cli.py` | Two-stage dispatch (the aws-clidriver lazy-command-table shape): the globals pass consumes the globals off the full argv, the command scan matches its remainder against `_COMMAND_TABLE` (the registry: name -> module, class, help - no command module imported), stage 2 imports the matched module, builds its real parser and runs it. Wires `--debug`, maps exceptions to exit codes; the full `build_parser()` builds the whole surface for tests |
| `globalargs.py` | Common option definitions (the parent; the aws-cli `globalargs.py` counterpart) |
| `clientfactory.py` | `build_client(args) -> S3Client` (the connection/authentication layer, section 4) + `build_service_client(service, args, *, region=None)` (the s3control / sts client used by mv's path validation, section 5.8). Both open their session, when none is handed in, through `build_session` - the botocore session `_open_botocore_session` opens (the profile / retry / timeout / credential-cache alignment of section 4) - and create their client through `_create_client`, the single seam that installs the CONNECT pin - a seam rather than `_open_botocore_session` because `build_client` may be handed a session built elsewhere. The construction-time botocore errors are translated in one place for all three (`_construction_errors`, section 6). Every S3 client the first one builds carries aws's message rewriter (`s3errormsg`) |
| `proxytunnel.py` | The CONNECT request the official aws distribution sends (section 2): a copy of CPython 3.12+'s tunnel opener, installed onto urllib3's `HTTPConnection` by `clientfactory._create_client` on the host interpreters whose own opener still writes `CONNECT ... HTTP/1.0` with no `Host`. Every precondition - no urllib3, no opener to read, an opener that already writes aws's shape - declines quietly, so it is a no-op on 3.12+ and idempotent |
| `s3errormsg.py` | aws's `after-call.s3` handler (its `s3errormsg` customization), ported verbatim: it rewrites two "requires Signature Version 4" messages and the cross-region `PermanentRedirect` in place, so every report carrying a service message - the top-level `[ERROR]` line and the per-item `upload failed:` / `download failed:` lines alike - reads as aws's. aws registers it on its session; here `build_client` registers it on each S3 client it builds, the CLI's only S3 client builder. The library stays boto3-faithful and never rewrites a service message |
| `checksumdefault.py` | aws's default request checksum (`CRC64NVME`, its bundled botocore's `DEFAULT_CHECKSUM_ALGORITHM`) reproduced on the CLI side: `default_algorithm(client)` feeds an upload run's `TransferOptions` (section 5.7, `--checksum-algorithm`), and `register(client)` - attached by `build_client` - stamps it at `provide-client-params.s3.*` on every other request whose operation names a `ChecksumAlgorithm` member, exactly where botocore would stamp its own (under `when_required`, only the checksum-required operations). Only where botocore can compute CRC64NVME. The library keeps botocore's default |
| `commands/base.py` | The `Command` ABC + `Context` (the injection point for runtime dependencies, section 3.1) |
| `commands/<sub>.py` | The `Command` subclass for each subcommand (e.g., `LsCommand` in `ls.py`, `RmCommand` in `rm.py`) |
| `commands/transferargs.py` | The surface shared by cp / mv / sync: the declaration equivalent to aws-cli `TRANSFER_ARGS` (`--expected-size` is cp-only opt-in, `--recursive` is opt-out for sync), validation of the SSE-C pair / checksum path types / case-conflict / S3 Express, conversion to `TransferOptions`, the non-stream location wiring (including the `--source-region` clone), the `[s3]` read and the engine's construction (`build_transfer_engine`, section 8), and the tail of exit-code derivation (`finish_transfer`, with `report_failed_run`, which rm shares) |
| `filters.py` | The order-preserving action for `--exclude` / `--include`, appending `Exclude` / `Include` rules to one list, and `compile_filter`, which hands that list to `globsieve.compile` (`None` when there is none). aws's matching - each side's base, the absolute and Windows drive forms, the Windows fold, the single-source forms - is the library's ([`globsieve.md`](./globsieve.md)); the two places it deliberately differs from aws (glob characters in the operation's path, cross-side application) are in docs/cli/aws-differences.md |
| `progress.py` | `TransferPrinter`: aws-compatible rendering of transfer result lines / progress (section 5.7-5.9. Worker-thread callbacks only count (the rc inputs) and enqueue slim records; a dedicated printer thread renders in queue order - aws's `ResultProcessor` shape, but with a **bounded** queue (aws-cli-option-handling.md section 6). The verb is `TransferType.value` - mv is `move` on every path. A record with no `dest` is rendered with a single endpoint - sync's `delete:` lines) |
| `shorthand.py` | Parsing of map-type option values (`--metadata k=v,...` / JSON form / the `@=` paramfile operator). The grammar is aws's, cut down to scalar values: it accepts exactly what aws accepts, and refuses while parsing the lists and nested maps aws parses and then rejects, as well as bytes from `@=fileb://`. Every refusal is the parser's `Error parsing parameter '<name>'` (252). A `{`- or `[`-led value is read as JSON, and only an object of strings is accepted |
| `paramfile.py` | aws's local paramfile loaders (`file://` text, `fileb://` binary; the `get_paramfile` counterpart) and its `load-cli-arg` handler over them: `load_argument` loads either prefix on an argument, `load_text_argument` on a string-typed one (a `fileb://` refused unread). The option and positional resolutions use the two; the shorthand `@=` operator calls `get_paramfile`. A load failure is a `ParamfileLoadError`, which every caller - the shorthand's `@=` included - turns into the named argument's rc-252 parse error through `named_argument` (the handler's boundary; aws's shorthand alone skips it and ends at 255) |
| `textencoding.py` | The CLI's text encoding, decided as aws decides it in the same environment whatever Python chose (design/overview.md section 3, "Text encodings"; section 1 below): `text_encoding` (UTF-8 under a `C` / `POSIX` `LC_CTYPE`, otherwise the locale's own encoding read past UTF-8 mode; the ANSI code page on Windows), `filename_encoding` (the same on Linux and the other non-Apple POSIX systems, `None` on Windows and macOS, whose names are Unicode / UTF-8 on both tools) handed to every `S3` / `LocalStorage` the CLI builds, the startup re-decoding of the command line and re-encoding of the standard streams, aws's `set_preferred_output_encoding` for the `[ERROR]` report, and the `os_path` / `text_path` conversions of a command-line path. Standard library only at import (it runs before parsing) |
| `output.py` | `aws s3`-compatible output formatting (`ls` listing lines, `rm` delete lines. Kept as pure functions; not turned into a class) |
| `usage.py` | The single home of the usage / error strings shared across subcommands (aws's own: `single_uri_usage` / `bare_single_uri_usage` / `two_path_usage`, and `param_error`, the `Error parsing parameter '<name>': ...` frame of aws's `ParamError`; this CLI's: `invalid_bucket_name_message`); commands interpolate only their own name, value or detail |

**Library consumption contract**: the CLI reaches `boto3_s3` only through its
public surfaces (the two-layer export contract of
[`imports.md`](./imports.md) section 2) - the package root's lazy re-export
(`boto3_s3.__all__`) and
the documented submodule surfaces (each module's `__all__`: the `transferplan`
planner,
`globsieve`, `localstorage.translate_os_error`, `awsconfig`'s shared size
core, `awsclicompare`, `crtsupport`, `masking`'s `SecretMaskingFilter`, and
`pathresolver`'s pin stand-down probes `is_mrap_path` / `is_outpost_path` /
`is_outpost_alias_path` / `is_s3express_path` / `is_s3express_accesspoint_path`,
`requestparams.validate_grants` (the `--grants` check the CLI makes before the
run), and `s3storage`'s `CA_BUNDLE_ENVIRONMENT_VETTED` mark).
What the in-repo CLI needs, any other application built on the library needs
too (the library is the product, overview.md section 1), so a CLI dependency
is met by *publishing* the symbol, never by importing a private one. Enforced
by `tests/cli/unit/test_library_surface.py`, which walks every `boto3_s3`
import (and module-alias attribute access) in the CLI sources against those
`__all__` contracts.

### 3.1 The subcommand interface (`Command`) and dependency injection (`Context`)

For testability (eliminating monkeypatch), subcommands are classes and receive
their dependencies through a `Context`.

- `Command` is an ABC (the `name` / `help` ClassVars, `configure(parser)`,
  `run(args, ctx) -> int`). Adding a subcommand requires only one subclass plus
  its registration in `cli._COMMAND_TABLE` (module path, class name, help line); no other wiring code is needed.
- Instances are **created anew** (per-run) at parser-build time and at dispatch
  time respectively. `run()` may keep its in-flight state (transfer counters,
  progress, etc.) in instance attributes, so calling `main()` multiple times in a
  single process does not carry state over (this corresponds to how aws-cli's
  `ListCommand._run_main` holds `self._total_objects` and the like).
- `Context` is the container for the runtime dependencies that `main()`
  resolves. Currently these are:
  - `client_factory` (`argparse.Namespace -> S3Client`, default `None` ->
    the command's `S3` builds the client on its session -
    `clientfactory.build_s3` / `build_client`; an injected factory builds it
    instead).
  - `s3_factory` (`argparse.Namespace -> S3`, default `None` -> `Context.s3()`'s
    standard construction; an injected factory takes precedence there) - tests
    inject it e.g. to share one session across a command's clients.
  - `service_client_factory` (`(service, args, *, region=None) -> client`,
    default `None` -> `clientfactory.build_service_client` on the command's
    session) - the injection point where mv's `--validate-same-s3-paths`
    creates the s3control / sts clients (section 5.8).
  - `transfer_config` (`TransferConfig | None`) - overrides the transfer
    engine's defaults; tests inject `use_threads=False` to make the multipart
    call order deterministic. **Only when it is `None`** do cp / mv / sync
    build the config from `[s3]` (section 8), so an injected value always
    takes precedence.

  Tests inject a fake client via
  `cli.main(argv, ctx=Context(client_factory=<fake>))` (no monkeypatching of
  module attributes).

## 4. Common options

These implement the policy in
[`aws-cli-option-handling.md`](./aws-cli-option-handling.md); the section
numbers in the bold item labels below are that document's.

- **Connection/authentication (effective, section 5)**: `--profile` -> the
  `profile` config variable of the botocore session every client is opened on
  (`_open_botocore_session`, item 5 below - not a
  `boto3.Session(profile_name=)`), `--region` / `--endpoint-url` -> client kwargs,
  `--no-verify-ssl` / `--ca-bundle` -> `verify`, `--no-sign-request` ->
  `signature_version=UNSIGNED` in the **session's default client config**
  (`_default_client_config`, where aws's own `no_sign_request` handler puts it -
  which is what leaves the credential chain's STS / SSO clients unsigned too,
  and what lets a per-client `signature_version` beat it, item 2 below; the
  CRT transfer lane takes the flag directly instead, as
  `S3(crt_sign_requests=not args.no_sign_request)` - aws's CRT factory reads
  its `sign_request` parameter and never the client's signature version),
  `--cli-read-timeout` /
  `--cli-connect-timeout` -> `Config` and the session's default client config
  (item 8 below). The assembled client is handed to the
  library via `S3Storage(uri, client=...)` (the library does not rebuild the
  connection settings). `verify` is never left to botocore to resolve later:
  `_resolve_verify` walks `--no-verify-ssl` > `--ca-bundle` > the `ca_bundle`
  config variable (`AWS_CA_BUNDLE` or the profile key, read off the session the
  client is built from) > `REQUESTS_CA_BUNDLE` > `get_cert_path(True)`, so every
  CLI-built client carries an explicit trust source. The first two steps are the
  option mapping; the rest reproduce, up front, the chain botocore would have
  walked at request time - which the CRT engine never sees, since
  `create_s3_crt_client(verify=None)` means the platform trust store
  ([`crt.md`](./crt.md)). Leaving it unresolved had the two engines trusting
  different roots. An empty or whitespace-only answer is **refused** rather
  than used: `_reject_empty_ca_bundle` raises botocore's own
  `InvalidConfigError` (rc 255), which is what aws v2 does
  from 2.36.6 on and what the installed botocore does from 1.43.54 - the
  supported floor is older, so the CLI supplies the check itself and the
  rejection holds across the whole range. aws runs it in every
  `create_client`, so it covers the clients the SDK builds from the session
  too - the credential chain's STS / SSO / sso-oidc clients and the CRT
  engine's request serializer, which read the session's `ca_bundle` even when
  `--ca-bundle` or `--no-verify-ssl` set the S3 client's own trust - and
  so does the CLI: every session it opens checks each client it builds
  (`_check_every_client`), and marks each with the
  library's `CA_BUNDLE_ENVIRONMENT_VETTED` so the library's own guess at an
  empty `AWS_CA_BUNDLE` (made for a client a caller hands it) stands down
  and refuses nothing aws would not. It runs where aws runs it, once
  `session.client` has built the client. `--no-verify-ssl` resolves to
  `False` ahead of the chain and is unaffected.
- **The alignment with aws v2**: `build_client` and the botocore session every
  client is opened on absorb eight differences between the installed botocore on
  a host interpreter and the botocore aws v2 ships frozen (the exception
  overview.md section 3 makes for a version difference that changes a normal
  run: items 1, 7 and 8 port aws-cli's own logic, the rest change a normal
  run's requests, profile or retries). A difference that shows only in an
  error or in an unusual environment - an unknown setting aws's botocore no
  longer reads, a value it refuses - is left to the installed botocore. Where a correction
  belongs to the *session* rather than to a client kwarg, that is deliberate:
  the session is what the clients botocore builds for itself - chiefly the STS
  / SSO clients the credential chain creates - are built from, and a per-client
  `Config` never reaches them.
  1. **region resolution** - aws v2 resolves the region as `--region` >
     `AWS_REGION` > `AWS_DEFAULT_REGION` > the profile's config `region` > the EC2
     IMDS region (its `_construct_cli_region_chain`), with the env vars
     present-wins. Stock botocore never adopted `AWS_REGION` (its region env is
     `AWS_DEFAULT_REGION` alone) and keeps its `IMDSRegionProvider` for
     smart-defaults only, so a bare client would resolve a *different* region
     whenever `AWS_REGION` is the sole source, or on an EC2 host with no
     configured region. `build_client` / `build_service_client` rebuild that exact
     chain in `_resolve_region` (reusing botocore's own `EnvironmentProvider` /
     `ScopedConfigProvider` / `IMDSRegionProvider`); an empty `AWS_REGION=`
     therefore selects the empty region too -> the same `Invalid endpoint` failure
     as aws (rc 255), not a fall-through to `AWS_DEFAULT_REGION`. The chain's
     answer is then **bound onto the session** (`_bind_region`), because handing
     each client a
     `region_name` never reached the STS client the assume-role /
     web-identity providers create for themselves: an assume-role profile whose
     only region source was `--region` or `AWS_REGION` signed `AssumeRole`
     against the global endpoint - a different partition under `cn-*` /
     `us-gov-*` - or failed outright with `NoRegion` where aws exits 0
     (measured). The already-resolved answer is bound rather than the chain
     re-installed, so the chain - whose last link is the IMDS probe - is still
     walked once per invocation. What binds is not always what the client is
     built with, because aws's **truthy guard belongs to the `--region` flag at
     the head of its chain, not to the chain's answer**: an s3 command hands
     `parsed_globals.region` straight to `create_client`, so `--region ""`
     reaches the client as the empty string (rc 255 on both tools) while the
     rest of the chain answers for the session - measured, `--region ""` with
     `AWS_REGION=eu-west-1` has aws sign `AssumeRole` in eu-west-1. A present
     but empty `AWS_REGION=` is a different case: it *is* the chain's answer, so
     it binds as itself and aws signs with an empty region scope, rather than
     being walked past to `AWS_DEFAULT_REGION` and the profile. Only the builder
     that opened the session binds it, and only from the `--region` the user
     typed: aws binds once, in `_handle_top_level_args`, and
     `--source-region`'s client leaves the session alone. Rebinding signed `AssumeRole` in a region
     aws never used, and with no other region source made a run aws fails
     (rc 1 / 253) succeed (measured). The library
     keeps stock botocore order on purpose (the boto3=library / aws=CLI split, as
     for the profile chain in item 5).
  2. **always-on SigV4** - stock botocore downgrades presigned URLs to SigV2 in
     regions that accept SigV2 (us-east-1), but SigV2 does not exist in aws v2's
     botocore. `Config(signature_version="v4")` is set for every command whose
     positionals name none of four targets (`"s3v4"` under `--sse aws:kms`, as
     aws sets it; the generic `v4` is the name aws's endpoint resolution hands
     its requests, which botocore's own `choose-signer` handler then turns into
     the S3 signer - a pinned `s3v4` left that choice to the next handler):
     an MRAP ARN, an S3 Outposts
     access-point ARN, an S3 Outposts access-point *alias*, and an S3 Express
     directory bucket. For those four
     the pin stands down: an explicit `signature_version` suppresses botocore's
     auth-scheme resolution, which must pick asymmetric SigV4a for an MRAP and
     for an Outposts access point in either notation
     (item 4) and `sigv4-s3express` (with `CreateSession` credentials) for a
     directory bucket - a pinned `v4` (or `s3v4`) matches either scheme name up
     to the first dash and would silently sign a plain SigV4 request instead. A plain,
     region-qualified access-point ARN is *not* in the set: it signs symmetric
     SigV4 like a bucket (every shape measured against the pinned aws-cli).
     The pin stands down for one further reason, unrelated to the targets: an
     **unsigned run**, so that the UNSIGNED waiting in the session default
     client config is what reaches the client (a per-client
     `signature_version`, this pin included, beats it in botocore's merge) -
     **except under `--sse aws:kms`**, the one case for which aws's own
     `ClientFactory.create_client` passes `Config(signature_version='s3v4')`.
     That combination therefore signs, resolves credentials and reaches the
     credential chain on both tools, while every other unsigned run stays
     anonymous (`_signature_version_pin`; measured without credentials across
     `cp` / `mv` / `sync` / s3-to-s3 `cp`, where aws is rc 1 `Unable to locate
     credentials` and sends nothing). `--sse AES256` and `--sse-c AES256`
     upload anonymously on both, and `mv`'s path-resolver clients stay
     anonymous throughout, since they name no `signature_version` of their own.
     aws's per-client config does not look at the target either, so under
     `--sse aws:kms` the four targets above sign plain SigV4 too, with the
     client's region in the scope, signed run or unsigned - the
     endpoint's own SigV4a / `sigv4-s3express` choice is overridden on both
     tools (`_requests_kms_signing`; measured in a network namespace with no
     route out, reading the signed request from `--debug`).
     **The gate is shaped after botocore's endpoint rules, not after aws-cli's
     resolver regexes**, because the two answer different questions - the
     regexes say which spellings `mv` can resolve to an underlying bucket
     (section 5.8), the rules decide the signer. `pathresolver._parse_arn_bucket`
     reads a bucket the way botocore's `aws.parseArn` does (six `:`-separated
     fields, the resource split with `:` folded into `/`), so an MRAP is
     recognized by an **empty ARN region** on an `s3` `accesspoint` resource
     rather than by a `.mrap` spelling, and an Outposts ARN by service
     `s3-outposts` with an `outpost/.../accesspoint/...` resource in any
     separator mix. The alias carries no ARN, so `is_outpost_alias_path`
     reproduces the rules' own positional test instead: the `--op-s3` suffix,
     the hardware-type character (`o` / `e`) 50 back from the end, the 17
     characters before it forming a valid host label, and a virtual-hostable
     name overall - a name the branch turns away is an ordinary bucket the
     rules sign plain SigV4, so the suffix alone would stand the pin down too
     often. All of it measured against the pinned aws-cli: 11 shapes that must
     sign SigV4a, 8 that keep the pin, and 7 that botocore rejects - the last
     group reporting the same on both tools whether the pin applies or not.
  3. **us-east-1 regional endpoint** - aws v2 resolves us-east-1 as regional
     (`s3.us-east-1.amazonaws.com`), its bundled botocore having dropped the
     `us_east_1_regional_endpoint` key from its `[s3]` table outright, so a
     `legacy` from `AWS_S3_US_EAST_1_REGIONAL_ENDPOINT` or the config key
     decides nothing there. The installed botocore still reads the key, so
     the value is pinned on the **session's config store**
     (`_pin_regional_s3_endpoint` / `_RegionalS3Section`): the wrapper forces
     the section's value to `regional`, because dropping the key would select
     the legacy global endpoint here (a missing key reads as `legacy`). The
     wrapper also passes botocore's smart-defaults write through to the
     provider it wraps (`set_default_provider`): a `defaults_mode` other than
     `legacy`, from the config key or `AWS_DEFAULTS_MODE`, sends that call at
     the `[s3]` section provider, and a wrapper answering only `provide()`
     failed *every* command at rc 255 where aws - whose bundled botocore has no
     defaults modes at all - simply ran. Delegating leaves botocore's
     bookkeeping intact and changes nothing the pin decides. The store is where
     every client reads the section, so mv's s3control / sts resolver clients
     and the credential chain's own are covered too, where a client `Config`
     covers only what this module passes it. It
     is not the whole story for a presigned URL, whose endpoint stock botocore
     resolves as if the region were `aws-global` regardless of this setting; the library's `presign`
     clears the flag that does it, so a URL from either layer names the
     client's own regional host like aws (docs/reference/operations/presign.md).
  4. **a pure-Python pin for symmetric SigV4 signing** - when awscrt is
     importable (it can be pulled in via the opt-in `crt` extra - transfer.md section 9 -
     or via a co-installed package), stock botocore swaps `v4` / `v4-query` /
     `s3v4` / `s3v4-query` for the CRT signer, changing the parameter order of
     presigned URLs (`X-Amz-Expires` moves after `X-Amz-SignedHeaders`). The
     botocore bundled with aws v2 pins these four to pure-Python classes and lets
     CRT handle only the asymmetric SigV4a family - `build_client` restores the
     same table (an in-place update of `AUTH_TYPE_MAPS`; with awscrt absent it is
     a no-op that just re-sets the defaults). Asymmetric SigV4a (MRAP ARNs and the
     like) is a domain where botocore itself requires awscrt: with the `crt`
     extra, the CRT signer handles it just as in aws v2 (measured: an MRAP
     presign signs `AWS4-ECDSA-P256-SHA256` with `X-Amz-Region-Set=*` on both
     CLIs - this needs item 2's MRAP stand-down, since the SigV4 pin would
     otherwise force a symmetric signature real AWS rejects); without it,
     botocore raises `MissingDependencyException` (`Missing Dependency: This
     operation requires an additional dependency. Use pip install
     botocore[crt] ...`), which the library translates to the plain
     `ConfigurationError` of the crt-absence family - rc 253 like the `[s3]`
     crt degradation (section 8), a state aws cannot reach (awscrt-dependent
     features are matched only where the CRT stack is usable - overview.md
     section 2). An S3 on Outposts access point is the
     quiet member of that family: its ruleset offers `sigv4a` *and* `sigv4`, so with awscrt absent
     botocore falls through to the symmetric scheme instead of raising, and the
     presigned URL comes back at rc 0 signed `AWS4-HMAC-SHA256` with no
     `X-Amz-Region-Set` (measured; recorded for the reader in
     [`compatibility.md`](../docs/compatibility.md) section 2).
     `BOTO_DISABLE_CRT`, the installed botocore's switch that hides an
     installed awscrt from it, puts botocore's signer and checksums in the
     same state (the CRT transfer engine, which imports awscrt directly, still
     runs, without the CRC64NVME default); aws's bundled botocore has no such
     switch, and the CLI leaves the variable to botocore (an unusual
     environment, overview.md section 3).
  5. **profile env precedence** - aws v2 resolves the active profile as
     `--profile` > `AWS_PROFILE` > `AWS_DEFAULT_PROFILE` > `default` (its bundled
     botocore lists the env vars as `['AWS_PROFILE', 'AWS_DEFAULT_PROFILE']`),
     whereas stock botocore reverses the last two
     (`['AWS_DEFAULT_PROFILE', 'AWS_PROFILE']` - the long-standing botocore #1725),
     so a bare `boto3.Session(profile_name=None)` would pick a *different* profile
     when both env vars are set. Every session the CLI opens comes from
     `_open_botocore_session`, which **redeclares botocore's `profile` config
     variable** with aws's env list and binds `--profile` onto the session
     instance only under aws's truthy guard (its `_handle_top_level_args`),
     leaving an env-named profile to botocore's own read. The two are not
     interchangeable: botocore reads a *session instance* profile as "the user
     asked for this one explicitly" and drops the environment credential
     provider for it (`create_credential_resolver`'s `disable_env_vars`), so
     baking the env-named profile in would turn `AWS_PROFILE=x` plus
     `AWS_ACCESS_KEY_ID` - a routine shape that works under aws - into `Unable
     to locate credentials`. Redeclaring the variable restores the order
     without that promotion, and the env read stays present-wins, an empty
     value included (`AWS_PROFILE=` -> the empty profile -> ProfileNotFound,
     matching aws). Everything that reads through a session inherits the
     order instead of restating it - the `cli_timestamp_format` /
     `cli_binary_format` check (section 6) and the `[s3]` scoped read
     (`load_scoped_s3_config`, section 8) included - so the transfer config
     and the client can never land on *different* profiles when both env vars
     are set. This correction is the CLI layer's alone: the library
     (`S3.client`'s `boto3.client` fallback) stays boto3/botocore-faithful and
     keeps stock order on purpose - the same library=boto3 / CLI=aws split as
     [`crt.md`](./crt.md).
  6. **retry defaults** - aws v2's bundled botocore hard-codes
     `retry_mode='standard'` / `max_attempts=3` as its session defaults, where
     stock botocore defaults to `legacy` with 5 total attempts - a visible
     difference under throttling. `_open_botocore_session` **redeclares both
     as session config variables** with aws's values (botocore's own env names
     and int conversion, so `AWS_RETRY_MODE` / `AWS_MAX_ATTEMPTS` and the
     profile config still win, present-wins - an empty value is fatal like
     aws, rc 255). Declaring them on the session is what puts the credential
     chain's own clients on aws's posture: a failing `AssumeRole` is attempted
     3 times and ends `(reached max retries: 2)` as under aws, where the
     per-client `Config` this used to be left it at legacy's 5 (measured).
     botocore then resolves and validates the whole retry configuration inside
     `create_client`, in its own vocabulary.
  7. **temporary credentials are cached on disk** - botocore caches what the
     `assume-role` / `assume-role-with-web-identity` / `sso` providers fetch in
     a per-process dict, so every invocation would re-call `AssumeRole` and an
     `mfa_serial` profile would prompt for a code every time - fatal (rc 255 on
     the `getpass` EOF) in any non-interactive run, where aws exits 0 from its
     cache. Two of aws's `session-initialized` handlers replace that dict with
     a `JSONFileCache` over `~/.aws/cli/cache`; `_open_botocore_session` does
     the same to every session it opens, at aws's timing (its event fires after
     the driver has bound `--profile`, so the provider chain built here is the
     run's own). The directory is aws's literal one and the cache key is
     botocore's, so the two tools reuse each other's entries - measured in both
     directions. The class installed is a `JSONFileCache` **subclass carrying
     aws's own write** (`_credential_cache_class`): the entry goes straight to
     its final path (`O_WRONLY | O_CREAT`, mode 0600, then truncate) instead of
     through `tempfile.mkstemp` + `os.replace`, so an unwritable
     `~/.aws/cli/cache` names the entry rather than a random `tmpXXXXXXXX.tmp`
     (rc 255 on both tools); and a parsed `Expiration` is serialized with
     `isoformat()`, the text aws's own timestamp handling leaves in the file.
     botocore's default renders a datetime with `strftime('%Y-%m-%dT%H:%M:%S%Z')`,
     whose `%Z` for an offset-carrying `datetime.timezone` is the literal
     `UTC+09:00` - which dateutil reads back with POSIX's inverted sign,
     leaving the entry looking valid for hours after it expired, for either
     tool sharing the directory.
  8. **the CLI timeouts reach the session** - aws resolves
     `--cli-read-timeout` / `--cli-connect-timeout` into the session's default
     client config as it binds the top-level arguments (its `_resolve_timeout`
     / `_update_default_client_config`, falling back to botocore's 60 s when
     the option is absent), so every client created from that session inherits
     them. `_default_client_config` builds the same value while the session
     opens, which is what holds the credential chain's own clients to the
     flag's budget: a stalled STS ran to botocore's own default here where aws
     gave up on the flag's (measured, `--cli-read-timeout 2` against an 8 s
     stall - rc 255 and a read timeout on both tools now, where this was rc 254
     carrying the server's own error). A value that is not an integer stops
     the run there (255); the clients this module builds pass the same two
     values in their own `Config`, which botocore merges on top - the same
     number either way.
- **Recognized and ignored (no-op, section 2)**: `--output` / `--query` / `--no-paginate`
  / `--no-cli-pager` / `--color` / `--cli-error-format`.
  They are accepted (the `choices` are validated) and have no effect on behavior.
- **auto-prompt (section 3)**: `--cli-auto-prompt` / `--no-cli-auto-prompt`
  are accepted and ignored; there is no interactive prompt.
- **`--debug`**: attaches a stderr handler to the `boto3_s3` / `boto3_s3_cli` /
  `botocore` / `boto3` / `s3transfer` loggers (via the library's
  `boto3_s3.set_stream_logger`, `mask_secrets=True`). `boto3_s3_cli` is the
  counterpart of aws-cli's own `awscli` logger, so the CLI's own debug lines
  surface too. Credentials (signatures, the
  access key id, the session
  token, proxy credentials) are masked by `SecretMaskingFilter` (design in
  [`masking.md`](./masking.md)). `urllib3` is excluded (because it does not emit
  credentials). Without a `set_stream_logger` call, the library attaches only
  a `NullHandler` (to the `boto3_s3` package logger).
- **`--version`**: prints a single line in aws-cli v2's User-Agent format and
  exits with 0 (in either position). `boto3-s3-cli/<v> boto3-s3/<v> boto3/<v>
  botocore/<v> Python/<v> <System>/<release>`. Both `boto3` and `botocore` are
  printed because the patch level can drift within boto3's pinned botocore range.
  Each distribution's version is computed individually via
  `importlib.metadata` (`unknown` when not installed). Because the standard
  version action wraps to the terminal width through the help formatter, a custom
  action that keeps it to a single line writes the output to stdout. The numbering
  policy is in [`overview.md`](./overview.md) section 3.
- **`--cli-binary-format`**: accepted as on aws and the `choices` (`base64` /
  `raw-in-base64-out`) are validated, but currently no command consumes it - a
  no-op. The blob argument of `aws s3 cp` (`--sse-c-key`) is
  **passed through** regardless of this setting (no base64 decode; only
  `fileb://` reads raw bytes), so cp is treated the same way.

Together, these accept all of the **7 ls-specific options (plus the positional
`<S3Uri>`) + 19 global** options listed by `aws s3 ls help` (`--version` prints
and exits, the other effective ones are reflected in the client / output, and the
rest are recognized and ignored or rejected). The list has been reconciled against
aws-cli's `awscli/data/cli.json` and `ListCommand.ARG_TABLE` as the primary
sources.

## 5. Subcommand implementation

The bucket part of an S3 URI may also be an access point ARN (regular /
Outposts), and just like aws-cli's `find_bucket_key`, the entire ARN (including
the `/`-separated name) is passed as `Bucket`. S3 Object Lambda / Outposts
**bucket** ARNs are rejected by `S3Storage.validate()` (deferred from the
permissive construction), matching aws's rc 252. Both are implemented in the
library layer.

### 5.1 `ls`

Equivalent to `aws s3 ls [<S3Uri>]`. `<S3Uri>` may omit `s3://` (`S3Storage`
fills it in).

| flag | handling |
|---|---|
| `--recursive` | `S3.ls(recursive=True, on_entry=...)` (recursive listing, no `Delimiter`. Ineffective in bucket listing = same as aws) |
| `--page-size N` | `S3Storage(uri, page_size=N)` (the listing page size is the storage's own config now, not an `ls` argument). No range validation (passed straight to the server as in aws: 0 yields 0 entries -> rc 1, a negative value yields `InvalidArgument` -> rc 254) |
| `--request-payer [requester]` | `S3.ls(request_payer="requester")` (ineffective in bucket listing = same as aws) |
| `--human-readable` | Formats the size in base-2 (CLI side, the library is not involved) |
| `--summarize` | Total Objects / Total Size at the end (CLI side) |
| `--bucket-name-prefix PREFIX` | `Prefix` of `ListBuckets` for bucket listing (ineffective in object listing = same as aws) |
| `--bucket-region REGION` | `BucketRegion` of `ListBuckets` for bucket listing (same as above) |

When the target has no bucket name (`boto3-s3 ls` / `ls s3://`), it lists **all
buckets**. The CLI normalizes every empty-bucket target to the bare `s3://`
service root first - including `s3:///k`, whose leftover key aws-cli also drops
(the library itself rejects `s3:///k` as malformed; the CLI owns this aws quirk).
`S3.ls` then dispatches the (already-normalized) service root to the separate
`S3Storage.list_buckets` (aws-cli splits its bucket listing from the object
listing the same way), which yields entries of `FileKind.BUCKET` (`mtime` =
CreationDate). `scan` itself is object listing only, so a transfer never sees a
bucket entry.

Output (`aws s3 ls`'s own, output-equal under the output comparison criterion
of [`testing.md`](./testing.md) section 9):

- Objects: `YYYY-MM-DD HH:MM:SS` (local tz) + size (right-aligned 10) + name
  (non-recursive = basename / recursive = full key).
- Common prefixes (non-recursive only): `{'PRE':>30} <name>/`.
- Buckets: `YYYY-MM-DD HH:MM:SS` (CreationDate, local tz) + bucket name (no size
  column).
- Exit code: 1 when a key / prefix is specified and there are 0 entries, 0
  otherwise (aws's `_check_no_objects`. Bucket listing is always 0, even with 0
  entries or a non-matching `--bucket-name-prefix`).
- A timestamp the local calendar cannot hold (within the zone's offset of the
  end of year 9999 or the start of year 1; on Windows, before 1970 or past
  about 3001) is listed by the library as the UTC value it is, but the line
  renders local time, so `ls` stops at that entry: the lines ahead of it are
  already printed, and the conversion's error is reported at 255. aws stops
  at the same entry, its own conversion failing where it reads the listing.
  The transfer commands, which render no stamp, carry on (overview.md
  section 3).

### 5.2 `rm`

Equivalent to `aws s3 rm <S3Uri>`. As in aws, rm's path validation is strict: a
non-`s3://` path is rc 252 ("Invalid argument type").

rm is an `S3TransferCommand` in aws, so it follows the family's construction
(section 5.7), its deletes riding the engine that construction builds: the
path check above and the normalization that follows it (section 5.7: a path
that names no bucket, the refused ARN forms, the keyless trailing `/`) come first, then the S3 client,
and then the `[s3]` runtime config is read and the transfer engine
materialized. An invalid `[s3]` value is
therefore 255 for every rm form, `--quiet` included (the report comes from
the error handler, not the result printer). A CRT selection builds the CRT
client there, and
the resolved `TransferConfig` is threaded into `S3.rm`, whose deletes ride
that client - batched - as aws's ride its manager ([`crt.md`](./crt.md)
sections 4 and 6), so the `[s3]` keys that tune the CRT client reach them on
both tools. On the classic engine the numeric keys stay inert for deletes:
the deleter paces itself (one batch in flight, its per-key requests ten at a
time), where aws's classic deletes are paced by `max_concurrent_requests` and
its queue size. Neither is observable in rc or output.

The target has 3 forms
(determined from aws-cli `filegenerator.py` plus the real
aws-cli's behavior):

- **Key specified (non-recursive) = a single blind DeleteObject**. It neither
  lists nor does a HeadObject, and even a nonexistent key "succeeds" (rc 0 + a
  `delete:` line). A `dir/`-terminated key is a blind delete of that folder
  marker object (objects inside are untouched).
- **`--recursive`**: normalizes the prefix to end with `/` and lists (aws's
  `FileFormat.s3_format`. Because `data` is listed as `data/`, a sibling such as
  `data-sibling.txt` with the same string prefix is **not swept in**). Folder
  markers are also deletion targets. The deletion is a batch `DeleteObjects` by
  the library's `S3Deleter`; keys that cannot be represented in its XML 1.0 body
  fall back to aws's per-key `DeleteObject` route
  ([`deleter.md`](./deleter.md) section 4).
- **No key (non-recursive) = folder-marker cleanup**: it lists everything but
  **deletes only the size-0, `/`-terminated markers** (at any depth). This is not
  a full wipe (a full wipe is `--recursive`).

| flag | handling |
|---|---|
| `--recursive` | The above (`S3.rm(recursive=True)`) |
| `--dryrun` | Calls no delete API, emitting only `(dryrun) delete:` lines (the recursive ListObjectsV2 still runs = a listing failure is fatal even under dryrun) |
| `--quiet` | **Suppresses all output** (not just success lines but also `delete failed:` / `fatal error:` lines. aws does not create the printer at all. The rc is unchanged) |
| `--only-show-errors` | Suppresses only success lines. **dryrun lines do appear** (an aws quirk: `OnlyShowErrorsResultPrinter` does not suppress dryrun) |
| `--exclude` / `--include` PATTERN | Evaluated in command-line appearance order, last wins (a shared dest of the same shape as aws's `AppendFilter`). `cli/src/boto3_s3_cli/filters.py` compiles the rules and passes them to `S3.rm(filter=)`, which matches each key below the target's base (`rm_filter_root`). Glob characters in the target path are taken literally, where aws reads them as part of the pattern (docs/cli/aws-differences.md) |
| `--request-payer [requester]` | Applied to both ListObjectsV2 and DeleteObject(s) |
| `--page-size N` | `S3Storage(uri, page_size=N)` (storage config, as for ls). No range validation (same policy as ls). However, when the server rejects the listing for a negative value, the exit code is **1 for rm** (fatal. Different from ls's 254 - section 6) |

Output: stdout `delete: s3://bucket/key` / `(dryrun) delete: ...`, stderr
`delete failed: s3://bucket/key <exc>` (a per-key failure) / `fatal error: <msg>`
(an error that halts execution). A line the stream cannot take at all - a
closed or full stdout, a reader that closed the pipe - is **dropped with a
debug log and does not reach the rc**: `_DeletePrinter` wraps each write the
way aws's `ResultProcessor._process_result` wraps each handler call, and by
then the deletes have happened, so the run keeps the outcome they gave it
(measured: `rm --recursive 1>&-` is rc 0 with the objects gone on both tools).
It covers the three lines the printer writes - success, dryrun and per-key
failure. A full stdout or a reader that left still ends the run at 255 once
the deletes succeeded: the lines stdout refused are found by the final flush
(section 6), not by the printer. **The output order of the delete lines is
non-contractual** (aws is non-deterministic, ordered by the parallel completion
of transfer futures. Tests use sort normalization + a final-state comparison,
testing.md).

A URI that names no bucket (`rm s3://` / `rm s3:///key`) is a usage error
(252), refused with the path normalization before the client is built
(`base.refuse_bucketless`, section 5.7). aws sends `Bucket=""` to the API
and fails there: a fatal error at the listing (rc 1, `--dryrun` included), a
per-key failure for the single delete (rc 1), and a `(dryrun) delete:` line
at rc 0 for the single delete under `--dryrun` (measured). A
recursive run with 0 matches is rc 0 and silent (unlike ls's rc 1, there is no
equivalent of `_check_no_objects`).

### 5.3 `mb`

Equivalent to `aws s3 mb <S3Uri>` (aws-cli `MbCommand`; a direct descendant of
`S3Command` that does not go through the transfer-family CommandArchitecture). A
non-`s3://` path is rc 252. **The key part is silently dropped** (same as aws's
`split_s3_bucket_key` - `mb s3://b/k` creates bucket `b`). A bucket name ending
in `--x-s3` (an S3 Express directory bucket) is rejected with rc 252.

Request shaping is done by the library's `S3.mb` in the same shape as aws-cli:
`LocationConstraint` is the client's region (not sent when it is `us-east-1`); if
the bucket name ends in `-an`, `BucketNamespace=account-regional`; tags go to
`CreateBucketConfiguration.Tags`. On a botocore whose model predates either
member, botocore's own parameter validation refuses the request before it is
sent, which the command reports on its `make_bucket failed:` line (rc 1).

| flag | handling |
|---|---|
| `--tags KEY VALUE` | Repeatable, appearance order preserved. **Duplicate keys are sent as-is too** (rejection is the server's responsibility = same as aws) |

Output: stdout `make_bucket: <bucket>` (the bucket name only) / stderr
`make_bucket failed: <path> <msg>` (the original path argument). **Any error
after the operation begins is uniformly rc 1** (both `BucketAlreadyOwnedByYou`
and a credential error at request time. aws locally catches every exception from
create_bucket - section 6). The success line is outside that catch: the
bucket exists once `s3.mb` returns, so a stdout that cannot take
`make_bucket: <bucket>` (full, a closed pipe) is lost output - the write error
at rc 255, as for every command (section 6) - and a process started without
a stdout (`1>&-`) writes nothing and keeps rc 0 (`output.write_result_line`).
aws's catch spans the line's `uni_print`, so there it is one more mb failure
(`make_bucket failed:`, rc 1, measured) for a bucket that was created.
`mb s3://` / `mb s3:///k` names no bucket and is refused before the client is
built (252, `base.refuse_bucketless`), where aws sends `Bucket=""` and
reports an mb failure at rc 1.

### 5.4 `rb`

Equivalent to `aws s3 rb <S3Uri>` (aws-cli `RbCommand`). A non-`s3://` path is
rc 252. **A key on a bucket is also rc 252** ("Please specify a valid bucket
name only. E.g. s3://<bucket>"). A bare trailing slash in `s3://b/` is
allowed, treating the key as empty = same as aws. A path that names no bucket
(`s3://`, `s3:///k`) is refused before the client is built (252,
`base.refuse_bucketless`), `--force` or not; aws sends `Bucket=""` and reports
an rb failure at rc 1, or under `--force` runs the inner rm first and ends at
255.

| flag | handling |
|---|---|
| `--force` | **Before** delete_bucket, it internally runs the entire `rm <S3Uri> --recursive` (delegating to `RmCommand` - `delete:` lines also appear the same as rm. aws likewise re-enters `RmCommand`). The inner run shares rb's `S3` (`Context.with_s3`), as aws's `RmCommand(self._session)` shares the one CLI session - credentials resolve once, so a `credential_process` / MFA flow does not re-prompt and the object deletes run under the same identity as the final delete_bucket. If rm's rc != 0, the fixed text "remove_bucket failed: Unable to delete all objects in the bucket, bucket will not be deleted." is raised as an `InvalidValueError`, so it reaches stderr through `main`'s general handler with its `boto3-s3: [ERROR]: ` prefix rather than a direct write - **rc 255** (the path where aws's own `RuntimeError` reaches its general handler the same way, `aws: [ERROR]: `). Every failure of the inner rm ends there, its span reporting by position rather than by exception type (section 6), so this is the only report a `--force` deletion failure produces. It does not attempt to delete the bucket |

Output: stdout `remove_bucket: <bucket>` / stderr `remove_bucket failed: <path>
<msg>`. A delete_bucket failure (`BucketNotEmpty` / `NoSuchBucket`) is
uniformly **rc 1**. The success line is
outside that catch, as mb's is (section 5.3): the bucket is gone by then, so
a line stdout cannot take is lost output (rc 255) rather than an rb failure,
and no stdout at all keeps rc 0.

### 5.5 `presign`

Equivalent to `aws s3 presign <S3Uri>` (aws-cli `PresignCommand`; a direct
descendant of `S3Command`). **It does not communicate with the server at all**
(only the local signature computation of `generate_presigned_url`). As in aws,
`s3://` is **optional** (the `bucket/key` form is also allowed - unlike mb / rb /
rm, there is no path-format check at all). The key is required: `presign s3://b`
/ `s3://b/` is botocore's client-side validation ("Invalid length for parameter
Key") -> rc 252.

| flag | handling |
|---|---|
| `--expires-in <seconds>` | Default 3600. **No range validation** (0 / a negative value / over 604800 are all signed as-is = same as aws. S3 rejects it only **when the URL is used**). A non-integer is rc **255** (the integer-conversion rule of section 6). On a SigV4a target (an MRAP ARN) the CRT signer refuses 0 / a negative value with awscrt's bare `AssertionError`; the library raises it as `InvalidValueError`, so it is rc **255** as on aws (where the general handler reports the assertion) |

Output: a single URL line to stdout. With `--no-sign-request`, a bare URL with no
query (matches aws). The signature format derives from the client
configuration, and `build_client`'s always-on SigV4 + us-east-1 regional (section 4)
makes it the same shape as aws v2. The SigV4 pin here is belt-and-braces: stock
boto3 downgrades a default client's presign to SigV2, and `S3.presign` already
pre-empts that downgrade itself for the duration of the one call, so the
library and the CLI produce the same v4 URL. What the CLI's pinned client
adds is only that the pin is visible in its own configuration.

rc forms: **0 / 252 / 253 / 255 only** (because the server is never reached, 1 /
254 cannot occur). Unlike mb / rb there is no local catch - botocore's
`ParamValidationError` becomes a `ValidationError` in the library and is 252 via
`main()`, credentials that cannot be located are 253 when signing (a missing
region is no failure: the client signs for us-east-1), a client-creation
failure is 255 for a present-but-unusable config (`InvalidConfigError`,
section 6), a non-integer `--expires-in` is 255, and so is the CRT signer's
refusal of a non-positive `--expires-in` on a SigV4a target (the table above).
A path that names no bucket (`s3://`, `s3:///k`, or the scheme-less `/k`) is
refused before signing by `build_s3_storage` (252, `base.refuse_bucketless`,
as on every command); aws signs with `Bucket=""` and botocore refuses the
empty name, also at 252.

### 5.6 `website`

Equivalent to `aws s3 website <S3Uri>` (aws-cli `WebsiteCommand`; a direct
descendant of `S3Command`, **no local catch**). It calls PutBucketWebsite once
and, on success, **outputs nothing** and is rc 0. The only options are
`--index-document <suffix>` (-> `IndexDocument.Suffix`) and `--error-document
<key>` (-> `ErrorDocument.Key`). **If neither is specified, an empty
WebsiteConfiguration is sent as-is** (it passes client-side validation; rejection
is the server's responsibility = same as aws).

Path handling follows the same procedure as aws's `_get_bucket_name`: `s3://` is
optional -> strip **exactly one trailing slash** -> treat the whole remainder as
the bucket name. Because aws **keeps a key (`s3://b/some/key`) folded into the
bucket name** and lets botocore's name regex reject it (rc 252), the CLI side
refuses the same names with a `ValidationError` naming the *unsplit*
remainder. Only the remainders whose rejection the split would otherwise hide
need that - every remainder the split does not keep whole as the bucket:
whatever `S3Storage` read as a key, the `/` left after stripping (`s3://b//`),
and an Outposts access point closed with `:` (`.../accesspoint/ap:`), whose
`:` the split eats as the key separator. A path that names no bucket
(`s3://`, `s3:///k`) is refused before that by `build_s3_storage` (252, as on
every command; aws rejects the name `/k` or the empty name, 252 too). Every
other name botocore refuses - a slash-free bad one (`b@d`, `s3://b b`) -
survives the split unchanged and rides on to PutBucketWebsite, where botocore
refuses it. So website has **two** 252 sources for a bad bucket name:
the one raised here (`usage.invalid_bucket_name_message`,
`Invalid bucket name "<name>"`), and botocore's at the request. An accesspoint
ARN passes through
entirely as `Bucket` (aws's `block_unsupported_resources` rejects only Object
Lambda / Outposts bucket ARNs = same as `S3Storage`'s parsing).

rc forms: **0 / 252 / 253 / 254 / 255** (because, unlike mb / rb, there is no
local catch: a server rejection - `NoSuchBucket`, an endpoint that does not
accept the configuration - is **254** derived from `ClientError`, while a
client-construction failure such as `ProfileNotFound` / `PartialCredentialsError`
is **255**. 1 cannot occur). The success path is verified on the dev stack
and on moto ([`testing.md`](./testing.md) section 3).

### 5.7 `cp`

Equivalent to `aws s3 cp <src> <dest>` (aws-cli `CpCommand`; transfer family =
`CommandArchitecture` + s3transfer). The engine design is in
[`transfer.md`](./transfer.md), the implementation in `commands/cp.py` +
`progress.py`. Routes are classified solely by the presence or absence of the
`s3://` prefix (upload / download / s3->s3 copy. local->local is a usage error
252). Path shapes - the meaning of an existing dir / a trailing-separator dest,
which of the two names to adopt, the bucket-root normalization of a keyless
`s3://bucket` - are derived by `boto3_s3.transferplan` (a port of aws's
`FileFormat`), shared between the CLI and the library. The filter bases are a
separate derivation (aws's `rootdir`), which the library's operations make
when they show each entry to the filter ([`globsieve.md`](./globsieve.md)
section 5).

**The construction of the transfer family** (cp / mv / sync / rm - aws's
`S3TransferCommand` subclasses) follows aws's stages. Each step stops the run
where aws stops it, with the code aws gives it; when several steps would fail,
which one is reported is not matched ([`overview.md`](./overview.md) section
3):

1. **The option values** - the paramfile expansions (252) and the integer
   coercions (255). The tokens the leaf parse left over (`Unknown options`)
   and `--query` were already checked by the dispatcher, right after the
   parse.
2. **The path-type gate and the normalization** - the local-local pair
   (252), then `_normalize_s3_trailing_slash` over the paths in order
   (`transferargs.normalize_s3_path`: a path that names no bucket, `s3://` or
   `s3:///k`, is refused first (252, `base.refuse_bucketless`; aws has no such
   check), then its `find_bucket_key` refuses the S3 Object Lambda and
   Outposts bucket ARNs (252), and a keyless `s3://` path
   gains the trailing `/` every later step re-reads, which turns an Outposts
   `.../accesspoint/ap:` into the key `/`). In `classify_paths` (cp / mv /
   sync) and in rm's `run()` (section 5.2).
3. **The S3 client** - aws's `S3Command._run_main`, which every `aws s3`
   subcommand inherits; built last in `classify_paths` and right after the
   normalization in rm's `run()`. A schemeless `--endpoint-url` (252), an
   undeclared profile (255) and an *empty* region (`AWS_REGION=`,
   `AWS_DEFAULT_REGION=`, or a config `region =`, whose endpoint resolves to
   `https://s3..amazonaws.com` -> 255) fail here. A merely *absent* region
   builds fine on S3's global endpoint.
4. **The rest of the path validation** - aws's `add_paths`: streaming, mv's
   onto-itself guard and resolver, the checksum / SSE-C pairings, the
   missing local source, then a directory source whose root the walk would
   skip (255, `transferargs.refuse_unwalkable_source`; aws runs on an empty
   source there), sync's directory-bucket rejection. Which of these a
   command runs is recorded with its command. aws's `_validate_path_args`
   also creates the `s3local` destination here; this CLI creates it right
   before the run, after every check including step 6's `--case-conflict`
   rejection, so a refused run leaves nothing behind.
5. **The `[s3]` runtime config and the transfer manager** - aws's
   `_get_transfer_manager`, `transferargs.build_transfer_engine`: the `[s3]`
   read (`transferargs.resolve_transfer_config`; an invalid `[s3]` value is
   255), then the engine's construction, which pays a CRT selection's whole
   construction even for a `--dryrun` ([`crt.md`](./crt.md) section 4).
6. **The run** - aws's `CommandArchitecture`: the `--case-conflict` x S3
   Express rejection (252), the `s3local` destination pre-create (255 on an
   OSError), then the transfer itself.

**The declaration surface is the full aws-cli ARG_TABLE**:
`--recursive` `--dryrun` `--quiet` `--only-show-errors` `--no-progress`
`--progress-frequency` `--progress-multiline` `--exclude/--include`
`--follow-symlinks/--no-follow-symlinks` `--no-guess-mime-type` `--content-type`
`--cache-control` `--content-disposition` `--content-encoding` `--content-language`
`--expires` `--metadata` (k=v,... / JSON) `--metadata-directive` `--copy-props`
`--acl` `--grants` `--storage-class` `--website-redirect` `--sse` `--sse-kms-key-id`
`--sse-c(-key)` `--sse-c-copy-source(-key)` (pair validation and the s3s3-only
restriction are 252, **the value is passed through** - aws
does not base64-decode. Only `fileb://` reads raw bytes)
`--force-glacier-transfer` `--ignore-glacier-warnings`
`--request-payer` `--source-region` (effective only for s3s3. The source client
swaps the region + discards `--endpoint-url` = aws-cli `ClientFactory`)
`--page-size`, streaming (`-`), `--expected-size`, `--no-overwrite`,
`--case-conflict`, `--checksum-mode`, `--checksum-algorithm`.

Two of these are **source-config**, not `S3.cp` / `mv` / `sync` arguments:
`--follow-symlinks/--no-follow-symlinks` and `--page-size` are baked into the
`Storage` the CLI builds - `resolve_locations` returns `LocalStorage(path,
follow_symlinks=…)` for a local side and `S3Storage(uri, page_size=…)` for an S3
side - because the library reads how a source is walked / listed from the storage
itself (`default_scan_options`), not from a per-operation argument. `ls` / `rm`
likewise build `S3Storage(uri, page_size=…)`. The CLI's Ctrl-C posture is
declared once instead: `build_s3` constructs the command's `S3` with
`reusable_after_interrupt=False` (Ctrl-C is process-fatal in the CLI, so a scan's
exit must not wait for an in-flight listing page pull, matching aws's
immediate death - the library default keeps waiting), and the operations
thread it into every scan they start ([`storage.md`](./storage.md) section 2).

**streaming (`-`)**: src `-` = stdin upload, dest `-` = stdout download (passing
`StdioStorage()` to the library. [`transfer.md`](./transfer.md) section 6). In
the form where the dest adopts the source name, the literal `-` becomes the
basename, per aws's naming (`cp - s3://b/pre/` -> key `pre/-`); this is derived in
transferplan.py before `S3Storage` is assembled. A run involving a stream **forces the
errors-only printer** (as in aws - it does not mix success lines or progress into
the raw bytes of a download). Combining `--recursive` is 252 (`Streaming
currently is only compatible with non-recursive cp commands`); stdout download +
`--no-overwrite` is also 252 (`--no-overwrite parameter is not supported for
streaming downloads`). An absent stdin is an in-pipeline fatal (`fatal error:
stdin is required for this operation, but is not available.`, rc 1). An absent
stdout has no counterpart check, aws guarding stdin only, so it fails the one
item instead: `download failed: s3://b/k to - 'NoneType' object has no
attribute 'write'`, rc 1 either way, an empty object included (transfer.md
section 6). A stdout that
cannot take the bytes - full, or a reader that left the pipe (`cp s3://b/k - |
head -c1` on an object larger than the pipe holds) - fails the item the same
way, whether a write or the flush as the item settles meets it:
`download failed: s3://b/k to - [Errno 32] Broken pipe`, rc 1. Bytes the pipe
accepted before its reader left count as delivered, so a small object there is
rc 0.
`--expected-size` (a multipart design hint) is **converted with a bare `int()`
just before the `S3().cp` call - inside the same in-pipeline boundary,
`finish_transfer` - and only on the streaming-upload route** (`src == "-"`)
- the only route aws ever converts it on (`UploadStreamRequestSubmitter`); on
every other route the value is untouched and ignored, exactly like aws, so a
non-integer there is **rc 0** (not converted). On the stream route a non-integer
is, unlike the 255 of the other integer options, an in-pipeline fatal of **rc 1**
(aws does a bare `int()` at submit time, section 6) - except under `--dryrun`,
which never converts it (rc 0 on both tools, measured). The
existence check for a single local src excludes `-`.

**`--no-overwrite`**: passed through to the library's `no_overwrite`
(transfer.md section 7). The server's PreconditionFailed answer to the
conditional write `--no-overwrite` sends (PutObject, CopyObject or
CompleteMultipartUpload) and an existing download dest are a **silent skip**
(rc 0, no line emitted). Any other PreconditionFailed - a multipart download's
or copy's part whose source changed since it was listed, a post-copy tag
write - fails its item (`... failed:`, rc 1), where aws skips every one of
them in silence.

**`--case-conflict`** (`ignore` (default) / `skip` / `warn` / `error`): passed
through to the library's case-conflict gate (transfer.md section 8). The skip / warn
messages go to stderr as a **NOTICE**, ahead of the printer's `--quiet` gate
(reproducing aws's direct `uni_print`. Not counted as warned; rc 0). `error` is
an in-pipeline fatal (rc 1). **S3 Express** (a bucket name ending in `--x-s3`,
s3local recursive only) branches separately: `skip` / `error` is a usage error
252 (`` `<value>` is not a valid value for `--case-conflict` when operating on
S3 Express directory buckets. Valid values: `warn`, `ignore`.``), while `warn`
emits a permanent warning to stderr and is downgraded to `ignore` (aws-cli
`_handle_case_conflicts`).

**`--checksum-algorithm`** (aws-cli's 9 choices: CRC64NVME / CRC32 / SHA256 /
SHA1 / CRC32C / SHA512 / XXHASH64 / XXHASH3 / XXHASH128) /
**`--checksum-mode ENABLED`**:
passed through to transfer.md section 9. Computing the CRT-family algorithms requires
awscrt, but it is not in the default dependencies and is an opt-in extra (the
delegation chain `boto3-s3-cli[crt]` -> `boto3-s3[crt]` -> boto3's own
`boto3[crt]`). In an environment without awscrt, only the CRT-family values
become an in-pipeline failure (rc 1) and diverge from aws (v2 bundles awscrt),
but this is allowed because awscrt-dependent features are matched only where
the CRT stack is usable (overview.md section 2, transfer.md section 9).
Signing stays pure-Python via the pin of section 4.

Without `--checksum-algorithm`, aws's default is its bundled botocore's
`DEFAULT_CHECKSUM_ALGORITHM`, `CRC64NVME`, stamped on every request whose
operation names a `ChecksumAlgorithm` member; pip botocore's is `CRC32`. The
CLI names aws's value (`checksumdefault.py`) in the two places botocore would
have stamped its own: an upload run's `TransferOptions` (`locals3` only - aws
sends no algorithm on a copy's requests, measured - so both transfer engines
see it; the CRT engine reads its trailing checksum from that argument alone),
and a `provide-client-params.s3.*` handler `build_client` attaches for every
other request (`rm`'s DeleteObjects, `website`, the post-copy `PutObjectTagging`, the annotation
writes). Only where the installed botocore can compute CRC64NVME (awscrt) and
only where botocore would have stamped a default of its own: on every such
request for a client whose `request_checksum_calculation` resolves to
`when_supported`, and under `when_required` only on the operations that
require a checksum (`DeleteObjects`, `PutBucketWebsite`, `PutObjectTagging`) - the
handler asks the operation model, in botocore's own order, so a
`when_required` `website` sends CRC64NVME on both tools while a
`when_required` classic upload sends none on either. The CRT engine is the
exception aws itself makes: its bundled CRT module carries CRC64NVME as its
own upload fallback and never reads the setting, so
`transferargs.default_upload_checksum` picks by the resolved engine and a
`when_required` CRT upload trails CRC64NVME on both tools (measured).
The library keeps botocore's default (crt.md section 1); the measured stake
is the CRT engine's 1 GB upload, where CRC32's software implementation cost
10% (benchmarks/RESULTS.md, 2026-09-06).

The checks of `run()`, in the order the code makes them (aws's stages; when
several fail, which one is reported is not matched): **the direct-option
paramfile loads and the two integer coercions**
(`resolve_paramfile_values`, `commands/transferargs.py`: the `--sse-c-key`
blob, the `--sse-c-copy-source-key` blob, `--grants` (a
lone `file://` / `fileb://` refused, and every grant checked for the
`<permission>=<grantee>` shape and a known permission, on every route: 252),
the free-string text block (`--sse-kms-key-id` among them, a `fileb://`
refused unread),
`--progress-frequency`, then `--page-size`; a load is 252, a bad `int()` is
255) -> **`--metadata` resolution (a whole-value `fileb://` refused 252,
paramfile load 252, shorthand parse 252)** -> cp's **`--expected-size`
paramfile (252)** -> the route type (local-local 252), a path that names no
bucket (`s3://`, `s3:///k`: 252, `base.refuse_bucketless` - aws sends
`Bucket=""` on: each upload or copy fails on its own (rc 1), a single
object's lookup or a listing is a fatal error (rc 1, `--dryrun` included),
and only a `--dryrun` upload passes, at rc 0) and the refused ARN forms (252) -> **client creation** (step 3 above: a schemeless
`--endpoint-url` is 252; an undeclared profile, an empty region or an invalid
configuration is 255, section 6; an absent region and missing credentials
are no construction failure, the latter fail the first request) ->
streaming constraints (252) -> **checksum path
type** (`--checksum-algorithm` is locals3 / s3s3, `--checksum-mode` is s3local
only, 252 - shared with mv) -> **a nonexistent single local
src (255**, equivalent to aws's bare RuntimeError.
`-` is excluded) -> **a `--recursive` local source whose root the walk would
skip (255**, `transferargs.refuse_unwalkable_source`: a file, a special or
unreadable path, a symlink under `--no-follow-symlinks` - the library's
walker decides; aws skips the root, with a warning or in silence, and runs on
an empty source, which a `sync --delete` turns into emptying the destination)
-> the SSE-C pair (252) -> the `[s3]` runtime config (255) and
the transfer-manager construction (253 on a CRT client the region cannot build)
-> the `--case-conflict` Express branch (252; taken for an access point of a
directory bucket too, which aws's `--x-s3` test misses) -> **the s3local
`--recursive` destination-directory pre-create (255** on an OSError - the
dir_op half of aws's `_validate_path_args`; sync shares it, unconditionally)
-> `S3().cp(...)`. The pre-create comes last, once every check has passed, so
a refused run leaves no directory behind; aws creates it in the middle of its
validation (right after the missing-source check) and leaves it there when a
later check refuses the run.
**`finish_transfer` - wrapping the `--expected-size` conversion and `S3().cp`
- is the in-pipeline boundary**: `BatchError` -> 1 (the `... failed:`
lines have already been emitted by on_result), a `KeyboardInterrupt` -> one
`cancelled: ctrl-c received` line + 1 (aws's result machinery swallows a
mid-run Ctrl-C into a cancelled run - measured mid-sync and mid-rm, 2.36.40;
rm's span converts through the same `report_failed_run`, and the
dispatcher's 130 backstop keeps the pre-pipeline spans - and the printer's
final drain, which is past aws's recorder span: a Ctrl-C there escapes to
aws's 130 while its
printer thread writes every queued line, so `TransferPrinter.finish` holds
the interrupt until the lines are out and then lets it reach the
dispatcher, whatever the run ended with). Any other library exception ->
a single `fatal error:` line + 1 (a single s3 src's HeadObject 404 = `Key "..."
does not exist`, a listing error, a non-integer `--expected-size`, an absent
stdin, and case-conflict `error` are also here; a malformed `--grants`, which
aws reports here as it maps the first upload or copy - not at all on a
download or a run with nothing to transfer - is refused before the run
instead, 252), a
normal return is **2** if the warned count > 0, else 0.

**Output** (`TransferPrinter`, aws-cli `ResultPrinter` shape): success
`upload|download|copy: <src> to <dest>` (stdout. the local side is rendered
relative to cwd = aws-cli `relative_path`, the s3 side is `s3://...`), a `(dryrun) `
prefix, failure `<transfer_type> failed: <src> to <dest> <err>` (stderr), warning `warning:
<body>` (stderr, the body assembled by the library with aws-cli wording). A
CANCELLED record (an item revoked by a fatal elsewhere or a Ctrl-C -
opresult.md) prints **nothing** (a fatal mid-listing prints zero per-item
lines for the cancelled set, as on aws), while every other record still
prints and counts after it: a failure that settles after the cancel keeps
its line and its cause, and a success that completed - an interrupted `mv`
whose source is already deleted - keeps its `move:` line. aws's
`ResultProcessor` disables every handler once it has processed the first
error result, so there those lines never print (an interrupted `mv` lost nine
`move:` lines in each of four measured runs); a failure is reported here
whatever happened around it (overview.md section 3). Progress
is `Completed <done>/<total> (<speed>/s) with <n> file(s) remaining`, overwritten
with `\r` (**no isatty gate** = mixed into a pipe too, as in aws. Goldens mask
it). The suppression matrix is the same shape as rm: `--quiet` = no printer at
all (**failures are silent too**, the rc is reflected), `--only-show-errors` =
suppresses only success/progress (dryrun appears. A run involving a stream forces
this), `--no-progress` = suppresses only progress. Only the **NOTICE**
(case-conflict's skip / warn messages) is outside the matrix: it goes to stderr
even under `--quiet` and is not counted (transfer.md section 8). aws's `~total
(calculating...)` display (the listing-incomplete marker) is not reproduced -
the library has no enumeration-finished signal to drive it - which is one of
the recorded progress-display deviations (option-handling section 6).

rc forms: **0 / 1 / 2 / 252 / 253 / 255**. 254 cannot occur (the transfer family
folds every error after the start into 1). 255 is for the pre-pipeline general
errors of the checks above - the timeout and integer coercions, client
construction (an undeclared profile, an empty
region, an invalid config), **a nonexistent single local src** (aws's bare
RuntimeError -> general handler), a directory source the walk would skip,
the destination pre-create, the `[s3]` config
and the transfer-manager construction.
The sources of a warning (rc 2): glacier skip, an mtime
stamp failure, an unreadable/special local file, a broken symlink, an invalid
mtime, a parent-ref escape, the pre-warning for a >48.8 TiB upload
([`transfer.md`](./transfer.md) section 8).

### 5.8 `mv`

Equivalent to `aws s3 mv <src> <dest>` (aws-cli `MvCommand`). The implementation is
`commands/mv.py` + `commands/transferargs.py` (shared with cp). **The transfer
surface is fully shared with cp (section 5.7)** - the declaration, validation, options
conversion, location wiring, output, and rc derivation all go through the same
code. This section records only the differences. The library side is `S3.mv`
(the cp pipeline + `is_move`, [`transfer.md`](./transfer.md) section 11).

**Differences in the declaration surface** (aws-cli ARG_TABLE: cp -
`EXPECTED_SIZE` + `VALIDATE_SAME_S3_PATHS`): `--expected-size` is not declared
(`Unknown options` 252). `--validate-same-s3-paths` is added. streaming is
**rejected post-parse in `run()`**: if either src / dest is `-`, it is 252 (`Streaming
currently is only compatible with non-recursive cp commands` - the message names
cp even for mv).

**mv-specific validation** (for s3s3, in `run()` after `classify_paths` has
built the session-backed `S3` **and its client** - step 3 of the family's
construction in section 5.7):

1. **The same-path guard** (always): if the keyless-normalized URI (`s3://b` ->
   `s3://b/`) matches `S3Storage.same_path` (an exact match, or a `/`-terminated dest
   + `basename(src)` concatenation equals src) -> 252 (`Cannot mv a file onto
   itself: <src> - <dest>`, displaying the normalized original URI). **`--recursive`
   is also subject to this** (`mv --recursive s3://b/d s3://b/` is 252 even when no
   key actually overlaps with itself - a faithful false positive of aws-cli.
   Confirmed by measurement).
2. **`--validate-same-s3-paths`** (the flag, or when the env
   `AWS_CLI_S3_MV_VALIDATE_SAME_S3_PATHS` is the **string `true`** - aws-cli
   `ensure_boolean` treats anything other than `'true'` (a lowercased comparison)
   as false. `=1` is invalid): only when `S3Storage.same_key` (a
   bucket-ignoring key comparison, including the `/`-anchored basename rule) is
   true, both sides are resolved to their real buckets with
   `boto3_s3.pathresolver.S3PathResolver` (access point ARN / alias / outposts
   ARN / MRAP ARN. A bare bucket passes through with no API call) and the
   same-path guard is reapplied to every combination - a match is 252 (the message
   uses the original URI). The clients go via `Context.service_client`:
   the src-side s3control uses `--source-region`, and when that is unspecified
   names no region at all, like sts: both then land on the session's region,
   which is a truthy typed `--region` (item 1 of section 4) or the rest of the
   chain - so `--region ""`
   resolves the source side through `AWS_REGION` and the like, as aws's does
   (its ARG_TABLE default is dead) - while the dest side's s3control is handed
   `--region` itself (a transcription of aws-cli `from_session`). An outposts **alias** is unresolvable, 252, and a missing MRAP
   alias is also 252. A ClientError from
   s3control / sts keeps `__cause__` and is **254** (aws is also 254 on
   a GetCallerIdentity failure). A resolver client that cannot be built
   stops the move before anything is transferred: with no region at all the
   s3 client still builds (global endpoint) and s3control's `NoRegion` is 253
   (section 6).
3. **A warning** (validation off and same_key and either side is an access-point form
   = `pathresolver.has_underlying_s3_path`): the aws-cli-worded permanent warning
   (`warning: Provided s3 paths may resolve to same underlying s3 object(s) ...`)
   is written directly to stderr and execution **continues** (no rc effect,
   appears even under `--quiet` - outside the printer).

Checksum path-type validation (`--checksum-algorithm` is locals3 / s3s3,
`--checksum-mode` is s3local only, 252) is shared by cp / mv (`transferargs.py`).

**The source deletion** is the engine's job, not the CLI's (transfer.md section 11): for
each successful item, an upload deletes the source through its `Storage.delete`
(`LocalStorage.delete`, an `os.remove`, since the CLI's upload source is always
local), while a download / copy does a DeleteObject against the source-side
client (RequestPayer passed through). On a
dryrun / a filter exclusion / a skip (no-overwrite, glacier) / a transfer
failure, the source remains, and **a failure of the deletion itself makes that
item a `move failed:` (rc 1)** (the bytes have already arrived). An emptied local
dir is kept (same as aws).

**Output** uses the mechanism of section 5.7 with only the verb being `move` (success
`move: <src> to <dest>`, `(dryrun) move: ...`, failure `move failed: ... <err>`).
The wording of the glacier warning stays the route word ("Unable to perform
download operations ..." - because aws-cli uses operation_name. transfer.md
section 8).

rc forms: in addition to the same **0 / 1 / 2 / 252 / 253 / 255** as cp, only a
resolution-API failure of `--validate-same-s3-paths` can be **254** (it occurs
before the operation begins = outside the transfer-exception rule).

### 5.9 `sync`

Equivalent to `aws s3 sync <src> <dest>` (aws-cli `SyncCommand`). The
implementation is `commands/sync.py` + `commands/transferargs.py` (shared with
cp / mv). The transfer surface, output, and rc derivation are shared with cp
(section 5.7), and this section records only the differences. The library side is
`S3.sync` (a two-layer pipeline + a pure pairing comparator - design in
[`sync.md`](./sync.md)).

**Differences in the declaration surface** (aws-cli ARG_TABLE: `TRANSFER_ARGS` +
metadata / copy-props / metadata-directive / case-conflict / no-overwrite + the
strategy-derived `--delete` / `--size-only` / `--exact-timestamps`): **`--recursive`
and `--expected-size` are not declared** (`Unknown options` 252 - sync is always
recursive and has no streaming form). `add_transfer_arguments(include_recursive=False)`.

**Validation** (the shared section 5.7 steps first - the paramfile loads /
coercions, `--metadata`, the route type, then the client build - and after
all of that):

1. Integer-option conversion (255; part of the shared steps above)
2. Route type: local->local is usage 252 (`usage: boto3-s3 sync <LocalPath>
   <S3Uri> or <S3Uri> <LocalPath> or <S3Uri> <S3Uri>` + `Error: Invalid argument
   type`), then a path that names no bucket and the refused ARN forms (252,
   the normalization of section 5.7)
3. If `-` is on either side, 252 (the same message as mv)
4. Checksum path type 252 (the cp / mv-shared `validate_checksum_paths_type` -
   aws-cli `_validate_path_args` applies to sync too)
5. A nonexistent locals3 src -> 255 (aws-cli `_validate_path_args`), and a
   local source whose root the walk would skip -> 255 (the cp row of section
   5.7; aws warns, or for a symlink under `--no-follow-symlinks` stays silent,
   and runs on an empty source - so `--delete` empties the destination)
6. SSE-C pair 252
7. **An S3 Express directory bucket or one of its access points on either
   side is 252** (`Cannot use sync command with a directory bucket.` - aws-cli
   `_validate_not_s3_express_bucket_for_sync`. The `--x-s3` suffix decision =
   `is_s3express_path`, plus `is_s3express_accesspoint_path` for `--xa-s3`
   (together `transferargs.lists_unordered`), which aws runs - and, its
   listing being just as unordered, loses objects to `--delete` - a
   deliberate difference (user decision
   2026-10-15, docs/cli/aws-differences.md). A **local** dir of the same name
   passes)
8. case-conflict resolution (treated as `recursive=True` - sync has no flag)
9. options conversion (`no_overwrite` is passed through in options; `S3.sync`
   reads it as the write-guard and strips it before the engine - sync does not
   attach IfNoneMatch. sync.md section 3)
10. The local destination-directory pre-create for a download (255 on an
   OSError - the same `_validate_path_args` half as cp's dir_op pre-create,
   run unconditionally for a local dest), after every check above: aws
   creates it at step 5's position and leaves it behind when a later check
   refuses the run

**The filter is compiled once**: the `--exclude` / `--include` sequence is turned
into a single `GlobFilter` (`filters.py`) and passed to `S3.sync(filter=)`,
which applies it to both sides, each entry matched below its own side's base
(aws's `rootdir`). A **relative** pattern matches each side's compare key
symmetrically, so "what the filter excludes is also excluded from `--delete`"
falls out (sync.md section 1); an **absolute** pattern replaces the base in
aws's join, so it prunes per-side - a source-anchored pattern leaves the
`bucket/key` destination visible, and `--delete` still removes it, exactly like
aws. aws also applies each side's joined patterns to the other side's
entries; that is not reproduced (docs/cli/aws-differences.md).

**Output**: the transfer lines are the same as section 5.7 (the verb is the route word
upload / download / copy). A deletion is `delete: <endpoint>` (**no `to` clause** -
the printer renders a record with no `OpResult.dest` as a single endpoint. local
is rendered relative to cwd) / `(dryrun) delete: ...`. The `--quiet` /
`--only-show-errors` matrix stays as the rules of section 5.7. The interleave order of
delete lines and transfer lines is non-deterministic in aws too (goldens use a
sorted comparison).

rc forms: the same **0 / 1 / 2 / 252 / 253 / 255** as cp (a deletion failure is
also aggregated into 1 by the transfer-exception rule. When src is a file, it is
2 from a walk warning; when dest is a file, it is 1 from a per-item `[Errno 20]` -
sync.md section 6).

## 6. Exit codes

Following [`overview.md`](./overview.md) section 3 (each code keeps the meaning
aws-cli gives it), these follow aws-cli v2's convention (aws-cli's
`awscli/constants.py`).

The specification belongs to [`exit-codes.md`](../docs/cli/exit-codes.md): what
each code means. This section records how that is produced - the mapping function, the per-family
catches, and the aws-cli counterpart each one mirrors.
[`exceptions.md`](./exceptions.md) section 5 and [`testing.md`](./testing.md)
section 2 point here for the mechanism. A change to either half needs the other
reviewed with it.

| code | condition | the name on the aws-cli side |
|---|---|---|
| 0 | Success. The `help` token / `--version` are also 0 | - |
| 130 | Ctrl-C **outside the transfer pipeline** (`KeyboardInterrupt` reaching `main`'s backstop: a bare newline on stdout, no traceback), the printer's final drain after the run included (section 5.7). Inside the rm / cp / mv / sync pipeline span a Ctrl-C is instead a cancelled run - `cancelled: ctrl-c received`, rc 1, like aws (section 5.7) | aws's `InterruptExceptionHandler`, 128+SIGINT |
| 1 | A subcommand-specific "no result" etc. (`ls` is a specified key / prefix with 0 entries), **all errors after the start of rm / cp / mv / sync / mb / rb** (below; the rm stage of `rb --force` is the 255 below) | the convention of the S3-family commands / a task failure of the transfer family |
| 2 | **A transfer that completed with warnings only** (cp / mv / sync's glacier skip, an mtime stamp failure, an unreadable local file, etc. section 5.7) | a task warning of the transfer family |
| 252 | A usage error (an unknown option = `Unknown options: ...`, an invalid choice / value), a client-side `ValidationError` | `PARAM_VALIDATION_ERROR_RC` |
| 253 | `ConfigurationError` (credentials / region unresolved, so their reports are enveloped and carry aws's hint (below); an absent awscrt x the `[s3] preferred_transfer_client=crt` degradation section 8 or an MRAP target's SigV4a section 4 item 4, which aws cannot reach and which stay bare), plus an unknown `cli_timestamp_format`, checked when a command opens its session (below) and enveloped as aws's `Configuration` report | `CONFIGURATION_ERROR_RC`, from its `NoCredentialsErrorHandler` / `NoRegionErrorHandler` / `ConfigurationErrorHandler` - three of its four rc-253 handlers, the `PagerErrorHandler` being the one with no counterpart here (below) |
| 254 | A server-side error (a `Boto3S3Error` whose `__cause__` is a botocore `ClientError`) | `CLIENT_ERROR_RC` |
| 255 | Any other general error (including `TransportError`, a `NotFoundError` with no `ClientError` cause such as a missing local source, the refining `InvalidValueError` / `InvalidConfigError` (below), an unparseable config / credentials file, an unknown `cli_binary_format` or an IMDS setting botocore refuses - each found when a command opens its session (below) - and any otherwise-uncaught exception via the backstop in `_run_command` / `main` - a `BrokenPipeError` from the `ls \| head` pipeline included, since aws has no special case for it either), **a failure of the rm stage of `rb --force`** (section 5.4), **a run that succeeded (0, or 2 with warnings only) but whose stdout output could not be flushed at the end** (below) | `GENERAL_ERROR_RC` |

**The code `main` returns is the one the process ends with.** Bytes a
standard stream refused - a full disk, a reader that left the pipe - stay in
Python's buffer, and the interpreter flushes the streams again as it exits: a
failure there replaces any exit code with 120 (aws ends there too). So `main`
flushes stdout and stderr before returning (`_settle_std_streams`), and a
stream that cannot be flushed is pointed at the null device (`os.dup2`,
Python's own recipe for a broken pipe), where the exit's flush lands
harmlessly. A run that already failed keeps its code, its failure reported (a
`download failed:` line, `ls`'s `[Errno 32] Broken pipe` at 255). A run that
succeeded (0, or 2 with warnings only) but whose stdout output was lost - the
result lines of a transfer or a delete the printer could not get out, or the
success line of `mb` / `rb` (section 5.3) - exits 255 with the cause (`boto3-s3: [ERROR]: [Errno 28] No space left on device`):
the output asked for was lost, a local I/O failure rather than a success. A
help page is written by `_print_help`, not argparse's `print_help`, which from
Python 3.11 swallows the write's `OSError` (a page larger than the buffer would
be lost at 0); a page stdout refuses ends at 255 either way - through `main`'s
backstop when the write itself fails, through the final flush when the page
was buffered.
A Ctrl-C during the final flush ends at 130 with both streams pointed at the
null device, so the exit's flush does not wait on the same reader again. A stdout that is
`None` (closed before the process started, `1>&-`) takes nothing and is left
alone; the code is the work's. A stderr that cannot take an error report or
be flushed, or that is `None` (`2>&-`), changes no code (`_write_error`
discards what it cannot write).

The mapping is `cli.exit_code_for`. It prioritizes "**whether the server was
reached** (whether it derives from `ClientError`)" over the library's exception
classification: even if the server returns a 400 and the library classifies it as
`ValidationError`, the exit code is 254 (because aws-cli treats every error after
reaching the server uniformly as `CLIENT_ERROR_RC`). With no `ClientError` cause,
the **refining subclasses are checked before their parents**: aws routes a
post-parse value failure (`InvalidValueError`) or a bad / unusable config
(`InvalidConfigError`) through its general handler, so both are 255 - not the
252 / 253 of plain `ValidationError` / `ConfigurationError`
([`exceptions.md`](./exceptions.md) section 2).

`_write_error` writes every report, in aws's default enhanced form whatever
`--cli-error-format` says
([`aws-cli-option-handling.md`](./aws-cli-option-handling.md) section 2.1).
A 252 report reads `An error occurred (ParamValidation): <message>` -
argparse failures, unknown options and plain `ValidationError` alike. The
**code the envelope names is the handler's, not the exception class's**, and
aws's rc-253 handlers also build their own message, so the two rc-253
failures reachable through botocore carry a hint botocore never wrote:
`An error occurred (NoCredentials): Unable to locate credentials. You can
configure credentials by running "aws login".` and `An error occurred
(NoRegion): You must specify a region. You can also configure your region by
running "aws configure".` (the hints are aws's verbatim - see
[`aws-differences.md`](../docs/cli/aws-differences.md)). `_configuration_report`
identifies that pair by the botocore exception the translation kept as
`__cause__` (or the exception itself, when one escaped untranslated), never
by matching the message text; the CRT region stop keeps a `NoRegionError` as
its cause for that reason. The third is `Configuration`, for the
`clientfactory.UnknownTimestampFormatError` an unknown `cli_timestamp_format`
raises (below). The rc-253 failures with no aws counterpart (an absent
awscrt, an SDK floor shortfall) stay bare, as do 254 (a `ClientError`
already carries its own `An error occurred (<Code>) when calling ...`) and
255 (aws's general handler formats nothing). aws drops the envelope when the
run names a profile no config file declares - its renderer fails to read
`cli_error_format` there and falls back to the bare message - and that
accident of its rendering is not reproduced: the envelope stays. Inside the
transfer pipeline nothing is enveloped: those failures never reach aws's
handler chain, so a credential-less run is rc 1 with botocore's own text - a
per-item `upload failed:` line when it lands on an object (cp / mv / a keyed rm), a
whole-run `fatal error:` line when it lands on the listing a recursive run
opens with (sync, `rm --recursive`).

Of aws's **two other rc-253 handlers**, only one is reachable here.
`PagerErrorHandler` (`Pager`) belongs to the output pager, which this CLI does
not implement. `ConfigurationErrorHandler` (`Configuration`) claims the
`ConfigurationError` aws's own customizations raise, and one of them lands
inside the mapped surface: `cli_timestamp_format` set to anything other than
`wire` or `iso8601` stops the run at rc 253. aws checks the setting when its
session starts, so it fails every invocation, `help` and `--version`
included. Here `clientfactory._reject_unknown_cli_formats` checks it, with
`cli_binary_format` (rc 255; an explicit `--cli-binary-format` replaces the
config value), every time `_open_botocore_session` opens a session - so every
command stops, while `help` and `--version`, which open none, succeed (the
accepted ordering difference of section 1). Both values come from botocore's
scoped config, so the profile is the one the session is bound to; an undeclared
profile makes that read raise `ProfileNotFound`, which stops the run at rc 255
as on aws. The timestamp report is aws's own text in the `Configuration`
envelope (`UnknownTimestampFormatError`, above); the binary-format one is
this CLI's own `InvalidConfigError` text, where aws prints the bare
`KeyError` its lookup raises.

The same session open is where the other config failures stop a command: a
config or credentials file botocore cannot parse (`Unable to parse config
file: <path>`, rc 255) and an IMDS setting botocore refuses while it builds the
credential chain (`metadata_service_timeout`, `metadata_service_num_attempts`,
`ec2_metadata_service_endpoint_mode`, `ec2_metadata_service_endpoint`; rc 255
with botocore's text), which `_inject_credential_cache` builds. aws reaches
both at startup too, so they also fail its `help` and `--version`.

The **accepted values change nothing on this surface**. On aws the setting
swaps botocore's timestamp parser for one that keeps the wire string (`wire`)
or returns an ISO-8601 string (`iso8601`), and its own `s3` customization then
re-parses whichever it got with a general date parser before rendering local
time, so both settings and the unset default print the same line. Measured
against the pinned aws-cli on a live endpoint: `ls`, `ls --recursive`, the
all-buckets listing and `--human-readable --summarize` were byte-identical
under `wire`, `iso8601` and no setting at all. So only the validation is
implemented; there is no parser to swap.

**`AWS_CLI_OUTPUT_ENCODING` re-encodes the error reports.** An unknown
codec stops the run at rc 255 before any command runs, as aws stops it. A
valid one is the encoding the `[ERROR]` reports are written in, and without it
`PYTHONUTF8=1` makes them UTF-8 - aws's `set_preferred_output_encoding`,
which its error handlers call and nothing else does (measured: result lines,
warnings and `fatal error:` lines keep the CLI's text encoding,
`textencoding.prepare_error_report`). The handler is `backslashreplace`
where aws's is `strict`, so a report the codec cannot represent is still
written rather than turned into another failure.

**The exception rule for rm / cp / mv / sync (the transfer-family commands)**:
aws-cli's transfer family (rm / cp / mv / sync) aggregates errors after the start
of the operation as a task failure / fatal error and makes them **uniformly rc 1,
even when server-derived** (the rc computation of `CommandArchitecture.run`. Both
a listing failure with NoSuchBucket and the InvalidArgument of `--page-size -1`
are 1). Therefore `RmCommand.run` / `CpCommand.run` / `MvCommand.run` /
`SyncCommand.run` catch what escapes the operation span themselves and return 1
(a per-item failure = `BatchError` -> the `... failed:` lines have already been
emitted by on_result, anything else = a `fatal error:` line), and do not let it
flow to `main()`'s `exit_code_for` (the 254 family). The catch is **positional,
not by type**: aws's recorder turns whatever the pipeline raises into an error
result, so a failure outside the library's taxonomy is the same one `fatal
error:` line at rc 1 as a `Boto3S3Error` (measured on both tools).
`KeyboardInterrupt` keeps its own arm
(the cancelled-run line above), the rest of the `BaseException` family passes,
and `AssertionError` is re-raised - the same carve-out the dispatcher's backstop
below makes. What becomes 252 is only a usage
error before the operation begins (a non-s3 path, an ARN rejection, cp / mv's
route type / SSE-C pair / mv's same-path guard, etc.). cp / mv / sync
additionally make a warnings-only completion rc **2** (aws-cli `CommandArchitecture.run`'s `failed>0
-> 1, elif warned>0 -> 2`). Only mv's `--validate-same-s3-paths` can reach the
server before the operation begins, so its resolution-API failure is plainly
**254**, outside the exception rule (section 5.8).

**The exception rule for mb / rb (the `S3Command` family)**: aws-cli's mb / rb
catch the exceptions of the API call within the command and make them uniformly
rc 1 (they do not turn it into 254 even when server-derived). `MbCommand.run` /
`RbCommand.run` likewise return 1 with a local catch. Both check the path's
scheme before they build the client. Client creation is
outside the local rc-1 catch: it translates botocore's construction-time errors
into the library taxonomy so they reach the exit-code mapping instead of escaping
as a traceback. The translation knows `NoCredentialsError` /
`NoRegionError` -> `ConfigurationError` = 253 (aws's dedicated handlers), but
neither arises at construction - the credential chain is walked there, and a
chain that finds nothing raises nothing until the first request is signed; an
absent region falls back to us-east-1 - so what construction raises is every
other `BotoCoreError` -> `InvalidConfigError` = 255 (aws's
`GeneralExceptionHandler`), including `ProfileNotFound` for a bad `--profile`
**and `PartialCredentialsError`** (e.g. an access key with no secret) - aws has
no handler for either, so both are 255, not 253. A schemeless
`--endpoint-url` is rejected as a usage error (252) when the client is
built. As a final
backstop, `_run_command` maps any non-`Boto3S3Error` exception that still escapes a
command to the same chain (credential/region 253, `ClientError` 254, else 255),
so no path crashes with a traceback + rc 1 - except an `AssertionError`, which
the backstop deliberately re-raises (a broken invariant crashes loudly). It
covers `--query` and `--endpoint-url` too, which aws resolves inside its
handler chain: an `--endpoint-url` whose netloc `urlsplit` refuses
(`http://[::1:9000` -> a bare `ValueError`) is the general report at 255 on
both tools, and a `--query` too deep to compile is the ParamValidation 252
(its `_resolve_query` catches bare `Exception`; both measured). `main` closes
the same chain around the dispatcher itself: whatever its own steps raise
that no step claims is reported through the same mapping, `AssertionError` excepted, as aws's entry point
reports whatever escapes its driver. The
rc-255 special case is the rm-stage failure of `rb --force` (section 5.4). Note that even among direct descendants of the same
`S3Command`, **website / presign have no local catch**: website's server
rejection is plainly 254 (section 5.6), and presign never reaches the server in the
first place (section 5.5).

**The conversion rule for integer options**: aws-cli converts integer-type
options (`--page-size` / `--expires-in` / `--progress-frequency`) with a bare
`int()`, and a failure (`ValueError`) reaches the general handler and becomes
**rc 255** (not 252).
Because argparse's `type=int` would turn the same error into
a usage error (252), it is not used; instead, each `run()` converts at the top via
`parse_integer_option` in `commands/base.py` (before the client factory),
raising `InvalidValueError` - the class
`exit_code_for` sends to 255. The CLI timeouts' `_coerce_cli_timeout` uses the
same class, where the session and client builders coerce them. **The exception is cp's `--expected-size`**:
because aws does a bare `int()` at submit time (within the pipeline) and **only on
the streaming-upload route**, a non-integer there is not 255 but a `fatal error:`
of **rc 1**; off the stream route, and under `--dryrun`, the value is never
converted, so a non-integer is rc 0 (section 5.7).

## 7. Import discipline (startup cost)

The top-level `help` token and `--version` exits load no AWS SDK module (boto3 /
botocore / s3transfer), no subcommand's command module (the `commands`
package's shared `base` infrastructure may load), and no library module beyond
the lazy `boto3_s3` root and its pure `exceptions`. This is the full CLI import
contract: after normal dispatch begins, SDK imports are permitted, including on
usage errors and subcommand help. [`imports.md`](./imports.md) defines the
contract and `tests/cli/unit/test_import_contract.py` enforces the two exits.
The key implementation points are:

- The `--version` line is assembled when the action fires, and the boto3 /
  botocore versions are read from the distribution metadata (the package proper is
  not imported).
- The help choices / help text are a static mirror of aws-cli's static
  tables (the same idiom as the `cli.json` mirror of section 4). They are not taken
  dynamically from botocore's models.
- Nothing ahead of the help token reads the config files (section 1), so
  the two exits stay SDK-free whatever the files hold.

## 8. Transfer-engine selection and the `[s3]` runtime config

cp / mv / sync / rm read the transfer settings from the profile's `[s3]`
section (`~/.aws/config`) and determine the transfer engine (classic / CRT).
The library does both for every run (`S3.resolve_transfer_config`, a port of
aws-cli's `transferconfig.py` in `boto3_s3.transferconfig`); the CLI calls the
same method at its engine slot (`transferargs.build_transfer_engine`) and
hands the result on, while rm only builds the engine (section 5.2). The
design is in [`crt.md`](./crt.md) section 4. The key points as the CLI sees
them:

- **Reading and validating `[s3]`**: `transferconfig.load_scoped_s3_config` reads
  it through the command's `S3.aws_config()` (an `AwsConfig` over the very
  session the client was built on, so it carries aws-cli's
  `AWS_PROFILE` > `AWS_DEFAULT_PROFILE` precedence for free - section 4 item 5;
  a bare `Session(profile_name=None)` would read a *different* profile's `[s3]`
  when both env vars are set), and
  `RuntimeConfig.build_config` converts sizes / rates / bools exactly as aws-cli
  `transferconfig.py`, resolves the `default` -> `classic` alias, and validates
  invalid values. An invalid value is the library's `InvalidConfigError`
  (rc 255 - aws-cli's class of the same name is also 255 at the general
  handler). Classic transfers read `multipart_threshold` and the like from the
  config through this same path.
- **The engine decision tree** (`resolve_transfer_client`, a port of aws-cli
  `TransferManagerFactory`): `s3s3` -> unconditionally classic; `preferred` is
  `classic` -> classic; `crt` -> crt if awscrt is present *and* the installed
  s3transfer carries the CRT surface (>= 0.8.0) - if either is missing, a
  plain `ConfigurationError` rc 253 (awscrt is an optional extra); `auto` -> crt
  if `is_optimized_for_system()` and the lock can be acquired (an s3transfer
  without the CRT surface silently resolves classic), otherwise classic.
  Streaming does not force classic.
- **`TransferConfig` construction** (`resolve_transfer_config`): pass the
  settings the caller's config gave, and for the rest only the keys
  explicitly present in `[s3]`, to the ctor (an unset one keeps boto3's
  `UNSET_DEFAULT` sentinel = "a CRT part_size only when `multipart_chunksize` is
  explicit" holds). The config is built **per engine** (the same as the aws-cli
  factory): classic gets all keys + `max_request_queue_size` +
  `max_in_memory_*_chunks=6`, while crt gets only the keys
  the CRT client reads
  and does not pass classic-only keys (`io_chunksize` / `max_bandwidth`, etc.)
  (matching the fact that aws's CRT ignores them + so as not to die in boto3's CRT
  config validation, crt.md section 4). The resolved engine is placed in
  `preferred_transfer_client` (so the library does not re-resolve `auto`).
- **Wiring**: each `run()` calls `transferargs.build_transfer_engine(ctx, s3,
  client, paths_type=...)`, whose `resolve_transfer_config` calls
  `S3.resolve_transfer_config`. A test-injected
  `ctx.transfer_config` supplies the settings it sets.
  `preferred_transfer_client` has no CLI option (config key only. Same as
  aws-cli).
- **No credentials or no region under CRT**: with no credentials aws enters
  the CRT engine anyway and fails each item inside its credentials delegate;
  the CLI keeps the library's boto3 rule, falls back to classic and fails the
  same items with `Unable to locate credentials` (crt.md section 4). With no
  region resolved, `build_s3`'s `S3` stops a CRT run at the engine's
  construction slot with botocore's missing-region error, rc 253, as aws's
  CRT run stops there (crt.md section 4, "Where the CRT region comes from").
  The lock posture is declared on `S3`: `S3(crt_allow_lockless=True)` makes an
  explicit `crt` preference build the CRT client even when another process
  holds the cross-process slot, aws's factory shape (crt.md section 4). The
  signing posture too: `S3(crt_sign_requests=not args.no_sign_request)`
  mirrors aws's CRT factory, which reads only its `sign_request` parameter -
  so `--no-sign-request --sse aws:kms` transfers anonymously on the CRT lane
  while the classic lane signs, exactly aws's engine split (crt.md section 4).
- **Annotation staging**: `build_transfer_options` always sets the library-only
  `AnnotationCopyMode.PRELOAD_MEMORY`. Thus `--copy-props all` reads every
  multipart source annotation before creating the destination, matching
  aws-cli, while no new CLI option is exposed. The tempfile/deferred modes are
  library-only.
- **SDK version difference (classic ranged download)**: aws validates the
  full-object checksum across the ranges of a classic ranged download (a
  feature of aws-cli's bundled s3transfer fork that pip s3transfer lacks), so
  under actual data corruption aws fails (rc 1) where we would report success.
  It comes only from the s3transfer difference and is not matched (overview.md
  section 3; the trigger conditions are in transfer.md section 10);
  non-ranged downloads (botocore) and the CRT engine (the CRT client) validate
  identically to aws.
