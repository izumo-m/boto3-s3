# aws-cli Option Handling

How `boto3-s3` (library) and `boto3-s3-cli` (CLI) treat each `aws s3`
option, including the ones that are recognized but do nothing and the
ones that are not supported at all.

The policy is uniform across all nine `aws s3` subcommands because they
share the same command machinery (see section 2.1).

Reference points:

- Reference CLI: **AWS CLI v2.35.5** (the pinned aws-cli). `aws s3` semantics
  rarely shift within the v2 line.
- The effective (functional) options and their mapping to S3 API
  parameters are derived from aws-cli's
  `awscli/customizations/s3/`, the canonical parity
  reference.

---

## 1. The two layers

Options are handled at two different layers, and "invalid" means
different things at each:

- **Library (`boto3-s3`)** sees only the *effective* options, as
  snake_case method parameters (e.g. `storage_class`, `sse`). It never
  sees presentation/transport CLI options. The only library-level
  "does not apply" case is an option that is valid only on certain
  transfer directions (section 4).
- **CLI (`boto3-s3-cli`)** is the surface that must accept the full
  `aws s3` flag set for parity. It maps the effective flags down to the
  library and is responsible for the options that have *no effect*
  (section 2) or are *not supported* (section 3).

## 2. Recognized-and-ignored (no-op) options - CLI

Several `aws s3` global options have **no observable effect** on any
`aws s3` subcommand. For parity, `boto3-s3-cli` **accepts them after
parsing and then ignores them** (validating `choices` where aws-cli
does, so an invalid value still errors the same way). Passing them
changes nothing about the operation.

| Option | Handling |
|---|---|
| `--output <fmt>` | Accept & ignore. Validates `choices`. |
| `--query <jmespath>` | Accept & ignore. |
| `--no-paginate` | Accept & ignore. |
| `--no-cli-pager` | Accept & ignore. |
| `--color {on,off,auto}` | Accept & ignore. Validates `choices`. |
| `--cli-error-format {legacy,json,yaml,text,table,enhanced}` | Accept & ignore. Validates `choices`. |
| `--no-cli-auto-prompt` | Accept & ignore (already the default; see section 3). |
| `--cli-binary-format {base64,raw-in-base64-out}` | Accept & ignore. Validates `choices`. |

### 2.1 Why these are no-ops

All nine `aws s3` subcommands are `BasicCommand` subclasses
(aws-cli's `awscli/customizations/s3/subcommands.py`:
`S3Command(BasicCommand)` -> `ListCommand` / `WebsiteCommand` /
`PresignCommand` / `MbCommand` / `RbCommand`, and
`S3TransferCommand` -> `CpCommand` / `MvCommand` / `RmCommand` /
`SyncCommand`). A `BasicCommand` returns an **integer exit code** from
`_run_main`, not a response payload
(aws-cli's `awscli/customizations/commands.py`). The CLI
driver's response-formatter pipeline therefore never runs, so
`--output`, `--query`, `--no-paginate`, and `--no-cli-pager` have
nothing to act on.

`--color` is a no-op for a related reason: the parsed `color` global is
consulted only by other parts of aws-cli (the table formatter, the error
handler, the ECS monitor commands), never by `aws s3`; the `aws s3`
customization
(aws-cli's `awscli/customizations/s3/`) contains no color handling
and prints its `upload:` / `download:` / progress lines unconditionally.
`aws s3 ... --color on` therefore produces no color, and `boto3-s3-cli`
matches that.

`--cli-error-format` controls the top-level CLI exception rendering;
`boto3-s3-cli` does not guarantee byte-for-byte console identity with
aws-cli (see section 6), so it accepts the flag and ignores it.

`--cli-binary-format base64` (the default) base64-decodes only the parameters
the *API operation model* declares as a `blob` shape:
`base64_decode_input_blobs` walks the operation's `input_shape` and
`Base64DecodeVisitor` decodes a member only when `shape.type_name == 'blob'`
(aws-cli's `awscli/customizations/binaryformat.py`). The `aws s3`
SSE-C keys reach S3 as the `SSECustomerKey` / `CopySourceSSECustomerKey`
request parameters, and both are **`string` shapes in the S3 model**, not
`blob` shapes - so they are never base64-decoded regardless of the setting. The
only transformation applied to `--sse-c-key` / `--sse-c-copy-source-key` is
paramfile loading (`file://` text / `fileb://` bytes), a separate mechanism.
(Their CLI argument definitions carry `cli_type_name: 'blob'` and a help text
that says the key "should **not** be base64 encoded" - `subcommands.py`:
`SSE_C_KEY`, `SSE_C_COPY_SOURCE_KEY` - but `cli_type_name` governs
help/validation, not the model-shape-driven decode above.) `boto3-s3-cli`
therefore hands the library the raw string (or the bytes read from a `fileb://`
path); the library accepts a raw `bytes` key and does no base64 step of its own.

aws-cli registers a global URI paramfile handler on every `aws s3` argument, so
`file://` (text) and `fileb://` (binary) load from disk for *any* arg, not just
the SSE-C keys. `boto3-s3-cli` matches this: `fileb://` (bytes) is accepted on
the two SSE-C key args, and `file://` (text) on those plus the free-string
transfer options (`--content-type`, `--website-redirect`, `--cache-control`,
`--content-disposition`, `--content-encoding`, `--content-language`, `--expires`,
`--sse-kms-key-id`) and `--metadata` (resolved before its shorthand parse). The
choices-validated options (`--acl`, `--storage-class`, `--sse`, ...) cannot carry
a `file://` value - argparse rejects it as an invalid choice first - matching the
practical aws result. A load failure (missing file, or a binary file via the
text `file://` prefix) is a usage error (rc 252) with aws's wording.

## 3. Auto-prompt / completion UI

`boto3-s3-cli` supports aws-cli's interactive **auto-prompt**
(`--cli-auto-prompt`) as an **opt-in extra**: it is active when
`prompt_toolkit` is installed (`boto3-s3-cli[autoprompt]`) and degrades to a
rejection-with-install-hint otherwise. The full design - the completion-candidate
spec, the port architecture, and the degradation - is in
[`autoprompt.md`](./autoprompt.md). This section records only the option-level
handling.

| Option / feature | Handling |
|---|---|
| `--cli-auto-prompt` (with `prompt_toolkit`) | Launches an interactive prompt with `aws s3`-style completion (usability-tuned, see below), then re-dispatches the completed command. |
| `--cli-auto-prompt` (without `prompt_toolkit`) | Rejected with an install hint and a non-zero exit code. |
| `--no-cli-auto-prompt` | Accept & ignore (already the default; section 2). Specifying it together with `--cli-auto-prompt` is a usage error, matching aws-cli. |
| `AWS_CLI_AUTO_PROMPT=on`, profile `cli_auto_prompt=on` | **Consulted** (env > profile config > off), matching aws-cli's chain. Read SDK-free (env + `configparser`). With `prompt_toolkit` it prompts; without it, config/env-driven prompting silently falls through to normal dispatch (only the explicit flag gives the install hint). |
| `on-partial` mode | **Supported**: run the command, and only on a usage error (rc 252) fall back to prompting (`autoprompt.md` section 5). |
| Shell completion (the `aws_completer` equivalent) | **Not provided.** |

The exit codes above are **not contractual**: the auto-prompt UI is an
interactive front-end, exempt from the exit-code charter (charter
exception 2 - `overview.md` section 3), and aws-cli itself fails when it cannot acquire
terminal control. The completion *candidates* are **not contractual either** -
the whole auto-prompt surface is charter-exempt, so it favors **usability over
aws parity** (it completes command-level choices like `--storage-class` that aws
misses, and widens reachability where aws's parser drops completions - see
[`autoprompt.md`](./autoprompt.md) section 2).

## 4. Direction-specific options

Some effective options apply only to certain transfer directions -
upload (local->S3, `U`), download (S3->local, `D`), or copy (S3->S3, `C`).
How aws-cli treats one supplied on a route where it does not apply is **not
uniform**: some combinations are rejected as a usage error, others are silently
dropped. `boto3-s3-cli` and `boto3-s3` mirror aws-cli case by case.

**Case (a): rejected with a usage error (rc 252).** A few flags are
path-format-validated by aws-cli before any transfer starts, so the wrong route
hard-fails. `boto3-s3-cli` mirrors each rejection (same message, rc 252):

| Flag | Allowed route(s) | Wrong-route behavior |
|---|---|---|
| `--checksum-algorithm` | `<LocalPath> <S3Uri>` or `<S3Uri> <S3Uri>` | rc 252 usage error |
| `--checksum-mode` | `<S3Uri> <LocalPath>` | rc 252 usage error |
| `--sse-c-copy-source` / `--sse-c-copy-source-key` | `<S3Uri> <S3Uri>` (copy only) | rc 252 usage error |

These mirror aws-cli `S3TransferCommand._validate_path_args` (checksum pairing,
in aws-cli's `awscli/customizations/s3/subcommands.py`) and
`_validate_sse_c_copy_source_for_paths` (copy-source scope, same file).
The CLI ports are `validate_checksum_paths_type`
(in `cli/src/boto3_s3_cli/commands/transferargs.py`) and the **copy-source
scope branch** of `validate_sse_c_pairing` (same file). The rest of
`validate_sse_c_pairing` (mirroring aws-cli `_validate_sse_c_arg`)
enforces that `--sse-c` / `--sse-c-key` (and the copy-source pair) are supplied
together; that pairing check is route-independent and belongs to neither
case (a) nor case (b).

**Case (b): silently ignored, not rejected.** The remaining direction-specific
options - `metadata_directive`, `copy_props`, `guess_mime_type`,
`case_conflict` - are accepted on any route and simply have no effect when the
route does not use them: either the corresponding S3 request parameter is never
emitted (`metadata_directive` / `copy_props` are copy-only; `case_conflict` is
download-only), or the route-specific logic just does not run (`guess_mime_type`
is an internal flag carried on every route but only consulted to infer
`ContentType` on the upload path). aws-cli applies the shared transfer arguments
uniformly and acts only on the route-relevant ones. `boto3-s3` does the same:
the internal snake->PascalCase translation only emits the parameters that are
valid for the chosen direction.

The full direction matrix follows aws-cli; the canonical reference is
aws-cli's `awscli/customizations/s3/`.

## 5. Effective options -> where they go

For completeness, the options that **do** have an effect:

- **Connection / auth** (`--profile`, `--region`, `--endpoint-url`,
  `--no-verify-ssl`, `--ca-bundle`, `--no-sign-request`,
  `--cli-read-timeout`, `--cli-connect-timeout`): `boto3-s3-cli` turns
  these into the boto3 client/session it hands to the library, which
  takes a ready boto3 client (e.g. via `S3Storage`) rather than
  rebuilding connection settings itself.
- **Object / transfer shaping** (`--acl`, `--storage-class`, `--sse`,
  `--metadata`, `--content-type`, ...): snake_case method parameters,
  translated internally to S3 API PascalCase (see the design record).
- **`--debug`**: the library emits via the standard `logging` module
  under the `boto3_s3` logger hierarchy; `boto3-s3-cli` wires a stderr
  handler. The library never attaches handlers itself.
- **`--version`**: prints a single aws v2-style version line
  (`boto3-s3-cli/<v> boto3-s3/<v> boto3/<v> botocore/<v> Python/<v>
  <System>/<release>`) and exits; handled entirely in `boto3-s3-cli`, with no
  library involvement.

## 6. Console output identity is not guaranteed

`boto3-s3-cli` does not guarantee byte-for-byte identity with
`aws s3` console output. Human-readable text - `--debug` traces, error
and warning messages, progress lines, `--help` - may differ in
wording, ordering, or exact content between the two tools and between
`boto3-s3-cli` releases. Parity is defined on S3 object state, return
values, and error conditions, not on console formatting.
