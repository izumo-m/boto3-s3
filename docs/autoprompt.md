# auto-prompt (`--cli-auto-prompt`) design

`boto3-s3 --cli-auto-prompt` provides the **interactive prompt + completion**
equivalent of `aws s3 --cli-auto-prompt`. It is enabled only in environments
where `prompt_toolkit` is installed; where it is absent, it explains how to
install it and refuses (the same **opt-in extra degradation** as awscrt =
the `crt` extra).

The implementation lives in `cli/src/boto3_s3_cli/autoprompt/`. The completion
engine is a faithful port of aws-cli's `awscli/autocomplete/`, narrowed to the
command surface of `boto3-s3`.

For the overall CLI design see [`cli.md`](./cli.md); for the cross-cutting
option policy see [`aws-cli-option-handling.md`](./aws-cli-option-handling.md)
section 3.

---

## 1. Position in the charter

The exit code parity charter ([`overview.md`](./overview.md) section 3) lists the
"interactive UI" as exception 2, placing it outside the scope of parity.
Therefore:

- Enabling auto-prompt imposes no exit code obligation (all rc values are
  non-contractual).
- The degradation (refusal) when `prompt_toolkit` is absent "does not count as a
  mismatch," just like awscrt.
- Console output (the appearance of the prompt and the completion menu) is also
  non-contractual
  ([`aws-cli-option-handling.md`](./aws-cli-option-handling.md) section 6).

Because auto-prompt is an interactive UI, **it is entirely outside the scope of
the charter**: in addition to rc and console output, **the set of completion
candidates is not a contract either**. We port aws's completion engine
(`autocomplete/`) as the foundation, but **we prioritize usability (completions
appearing naturally) over matching aws** (next section).

**Parity with aws's auto-prompt is an explicit non-goal.** Auto-prompt is
interactive and never driven from a script, so there is no parity value to
preserve here - and a completion that is *more* helpful than `aws s3`'s is itself
a reason to reach for `boto3-s3`. The guiding rule is therefore one-directional:
**where aws completes, we complete; where aws does not, we still may** when it
helps the user finish the command. We never complete *less* than aws. This is the
opposite of the exit-code charter (which demands exact parity) precisely because
the UI is charter-exempt.

## 2. Completion policy (usability first)

Byte-matching aws all the way down to behavior that depends on prompt_toolkit's
display quirks is impractical and of little value (the UI is non-contractual).
This implementation therefore builds on aws's engine while **applying
usability-first corrections wherever aws's parser/wiring quirks cause
completions to be missing or off-target**. Specifically:

- **Complete the values of every option that has choices** (derived from
  argparse's `action.choices`, without distinguishing global from
  command-level). aws **only completes the global choices (`--output`, etc.)** -
  it misses command-level customization choices because the value path for
  completion (`ShorthandCompleter`) only looks at `argument_model.enum` (empty
  for customization args). As a result, `aws s3 cp --storage-class <TAB>` yields
  no values, but **this implementation also completes `--storage-class` /
  `--acl` / `--sse` and the like**. This can also be described as "correcting
  aws's wiring gap," but **usability** is sufficient justification on its own
  (known valid values are better completed than not).
- **Widen reachability** (section 3). aws's parser assumes a single positional
  argument, so completions are missing or off-target for `cp --storage-class
  <TAB>` (no preceding path) or `cp src dst --<TAB>` (after the second path).
  This implementation adjusts the parser to correct these.

**Out of scope**: a feature aws **does not have** - server-side completion of S3
buckets/keys - is not added (that is a new feature rather than "usability," and
it is an aws-unsupported feature).

## 3. Completion candidate specification

The filter is auto-prompt's default **fuzzy** (subsequence match).

| Position | Candidates |
|---|---|
| Subcommand | `cp ls mb mv presign rb rm sync website` (the candidate set; display order is decided by the fuzzy filter) |
| Option name | the declared options of the relevant subcommand + global. `--no-X` is **only the explicitly defined ones** (not auto-generated from booleans). Parameters already entered are excluded |
| `--region` value | `boto3.Session().get_available_regions("s3")` (local data, no network) |
| `--profile` value | `boto3.Session().available_profiles` (local config) |
| `file://` / `fileb://` | local path completion only when the prefix begins with them (option-independent) |
| **values of every option that has choices** | global (`--output` `--color` `--cli-binary-format` `--cli-error-format`) + **command-level** (`--storage-class` `--acl` `--sse` `--metadata-directive` `--copy-props` `--checksum-algorithm` `--case-conflict`, etc.). As noted in section 2, aws does not complete the latter |

The `help` subcommand is not offered (this CLI has no `help` subcommand;
`--help` is also excluded because it is not in aws's candidate set).

### Reachability (parser adjustments for usability)

aws's `CLIParser` assumes a single positional argument, so as ported verbatim
the following do not appear. This implementation corrects them by adjusting
`_handle_positional` in `parser.py` at two points (the UI is non-contractual =
section 1).

- **Option value before a preceding path**: option-value completion at a stage
  where no path has been typed yet, as in `cp --storage-class <TAB>`. The source
  consumed the value into the positional, overwriting `current_param` and
  killing value completion -> fixed so that **the positional is claimed only when
  the fragment is non-empty AND no option value is awaited**. With this, (a) an
  option value stays with its option and is completed, (b) bare `file://` path
  completion is preserved (delegated to FilePathCompleter), and (c) an empty `cp
  <TAB>` does not claim the positional and offers all options just like `cp src
  <TAB>`.
- **Option after the second path**: cp/mv/sync take two paths, but the source
  set `current_args` to None at the second path and dropped subsequent options
  into `unparsed_items`, stopping completion -> fixed to **keep the command's
  options alive after the second positional, whatever it looks like**. So after
  *any* second positional - an `s3://` URI, a `./` / `/` / `file://` path, or a
  bare local name like `outdir` - option-name completion, value completion, and
  dedup (excluding already-present options) all work: an empty fragment offers
  all options and `--frag` narrows them. aws drops a bare (non-path-like) second
  positional on a path-likeness heuristic and offers nothing there; we do not -
  the completer defers only while an option value is being typed (so its value
  completer fires), and so never offers less than aws (section 1). The source
  also fuzzy-matched the options against the command name, offering a useless
  subset even for the path-like forms.

These are **deliberate deviations** from aws's behavior (usability first), and
they are guaranteed by
`tests/cli/unit/test_autoprompt.py::TestCompletionReachability`.

## 4. Architecture

Dispatch remains the existing argparse (this does not break the execution path
already guaranteed by the exit code charter / golden). Completion does not hack
argparse; it layers an independent completer pipeline on top.

| module | role | dependencies |
|---|---|---|
| `model.py` | introspects `cli.build_parser()` to build the completion model (subcommand names / option names / nargs / required / help / **choices** / positional arguments). Separates global from command-specific | pure Python |
| `parser.py` | a port of aws-cli `CLIParser` / `ParsedResult` (parses partial input without erroring). Normalizes the root to the two-level hierarchy of `boto3-s3`. `_handle_positional` is adjusted at two points for usability (section 3 reachability) | pure Python |
| `completers.py` | `CompletionResult` / `AutoCompleter` (adopts the first completer to return non-None) / `fuzzy_filter` plus each completer (name / region / profile / file:// / choices) | pure Python (botocore is lazily imported only when completing region/profile) |
| `prompter.py` | the `AutoPrompter` ABC (`prompt_for_args(argv)->argv`). The injection point | pure Python |
| `prompt.py` | the `prompt_toolkit` implementation. The `CompletionResult`->`prompt_toolkit.Completion` adapter (excludes auto-prompt flags + dedup + required first) and the `PromptSession` loop | **`prompt_toolkit`** |

**Single source = argparse**: the option set is derived from the argparse
definition, so it does not drift (guaranteed by the test
`test_offered_options_match_argparse_exactly`). The aws-parity option set itself
is inherited from the ARG_TABLE mirror that the dispatch parser already
upholds.

**Lazy import**: `model.py` / `parser.py` / `completers.py` are pure Python.
Only `prompt.py` imports `prompt_toolkit`, and that import happens only when
`--cli-auto-prompt` actually fires. The botocore used for region/profile
completion is likewise lazily imported when the completer fires. `--help` /
`--version` / usage / normal dispatch touch neither the `autoprompt` package nor
`prompt_toolkit` (the import contract, [`imports.md`](./imports.md); guaranteed
by `test_import_contract.py`).

The completer order (adopt the first non-None): Region -> Profile -> ModelIndex ->
FilePath -> Choices. ModelIndex returns None while an option value is being typed
(when `current_param` is set), so processing passes to the value completer.
aws's chain is Region -> Profile -> ModelIndex -> FilePath -> **serverside** ->
**Shorthand** -> **Query**, but in `aws s3` **serverside** (S3 server-side
completion) and **Query** (`--query` value completion) are no-ops (s3 is a
customization rather than a modeled operation, so it has no `operation_model`,
and `--query` / `--output` do not go through response formatting), so they are
not ported. **Shorthand** is replaced by **Choices** narrowed to choices (section 2:
uniformly complete every option that has choices).

Region / Profile value completion fires on every keystroke, so the resolved list
is cached for the completer's lifetime (= one prompt session) (recreating
`boto3.Session()` every time costs ~40ms per keystroke; aws also reuses the
session).

Display uses prompt_toolkit's standard completion menu. aws's full-screen
doc-panel app is not reproduced (candidate matching is the contract, UI chrome is
non-contractual).

## 5. Activation conditions (mode resolution)

`cli.main` resolves the mode from raw argv + env + profile config **before**
argparse (to slip past the subcommand-required argparse and fire even on a bare
`boto3-s3 --cli-auto-prompt`; equivalent to aws's `resolve_auto_prompt_mode` + config
chain = aws-cli `clidriver.py`'s `resolve_auto_prompt_mode` +
`_construct_cli_auto_prompt_chain`).

First, `main` rejects `--cli-auto-prompt` and `--no-cli-auto-prompt` together
with 252 (mutual exclusion, aws's wording) before mode resolution runs (in
`cli.main`, ahead of the `_resolve_auto_prompt_mode` call).
`_resolve_auto_prompt_mode` itself returns only a mode string
(`on` / `on-partial` / `off`), never an rc.

The precedence in `_resolve_auto_prompt_mode` (the first one decided wins):

1. If any of `--help` / `-h` / `--version` is present -> **off** (display
   help/version).
2. `--no-cli-auto-prompt` -> **off**.
3. `--cli-auto-prompt` -> **on**.
4. Otherwise -> env `AWS_CLI_AUTO_PROMPT` -> profile `cli_auto_prompt` -> `'off'`.
   The value is lowercased, and anything other than `on` / `on-partial` is
   treated as off (aws's else branch).

Behavior per mode:

- **on** -> run the prompt -> re-dispatch **exactly once** from the edited argv
  with the auto-prompt flags removed (to prevent an infinite re-prompt loop).
- **on-partial** -> first **run as-is**, and fall back to the prompt and
  re-dispatch **only when rc is 252 (usage error)** (the on-partial branch in
  aws-cli's `clidriver.py`). 252 is emitted before the S3 call, so it can be re-run
  without side effects. The usage messages from the attempt (argparse's usage
  block / `Unknown options` / 252 `ValidationError`) are **silenced**
  (`_dispatch(suppress_usage_errors=True)` = aws's
  `SilenceParamValidationMsgErrorHandler`) - so the prompt is not buried under a
  huge usage dump. Since argparse writes to stderr on its own inside parsing,
  only **the area around the parse** is discarded via redirect (the parse is
  instant, so live output is not lost). The command body runs with stderr live.
  Note that the partial trigger picks up rc 252 in general from the execution
  result (both the parser stage and the usage validation inside `run()`).
- **off** -> normal dispatch.

**config/env reading is SDK-free**: env comes from `os.environ`, and profile
config is read with `configparser` from the relevant section (`[default]` or
`[profile <name>]`) of `~/.aws/config` (`AWS_CONFIG_FILE` takes precedence).
Since botocore is not imported, the import contract is upheld even on the usage
error path (section 4, `test_import_contract.py`). The active profile is `--profile` >
`AWS_PROFILE` > `AWS_DEFAULT_PROFILE` > `default`. This is not botocore's full
resolution (abbreviations, nesting), but it is sufficient for this interactive
setting that is outside the charter.

**Difference when prompt_toolkit is absent** (section 6): an explicit flag gets the
install guidance + 252. The env/config-driven case **silently falls back to
normal dispatch** (a missing optional dependency must not break every command).

## 6. Degradation and non-contractual rc

When `ctx.auto_prompter` is not injected, it branches on
`importlib.util.find_spec("prompt_toolkit")` (existence check without
importing):

- **Absent + explicit flag** -> guidance on stderr (`... requires the optional
  'prompt_toolkit' dependency. Install it with: pip install
  'boto3-s3-cli[autoprompt]'`) + rc 252.
- **Absent + config/env-driven** -> **silently fall back to normal dispatch**. So
  that the combination of an ambient setting (`cli_auto_prompt=on`, etc.) and a
  missing optional dependency does not break every command.
- **Present** -> import `prompt.py` and run the prompt.

All rc values are non-contractual (charter section 1). Reference implementation values:
guidance refusal / mutual exclusion = 252, non-tty / prompt failure = 255, user
cancel (Ctrl-C/Ctrl-D) = 130. The argv returned by the prompt goes through
normal dispatch, so the subsequent rc follows each subcommand's convention.

## 7. Context injection and tests

`Context` in `commands/base.py` gains `auto_prompter` (`AutoPrompter | None`,
default None -> lazily created by `main`). Tests inject a fake `AutoPrompter`
(returns canned argv) to verify "seed -> prompt -> re-dispatch" without a tty
(the same DI style by which other subcommands inject `client_factory`; no
monkeypatching).

Test structure (`tests/cli/unit/test_autoprompt.py`):

- **Completion engine**: assemble `AutoCompleter` directly (region/profile use a
  fake provider) and verify subcommand names / option names / **command-level
  choices completion (gap correction)** / global choices / region / profile /
  file:// / fuzzy ordering / exclusion of already-present options. Pure Python,
  no tty required.
- **Drift guard**: each subcommand's completer option set == its argparse option
  set.
- **Wiring**: mutual exclusion 252, missing-dep (stub `find_spec`) guidance +
  252, re-dispatch via an injected prompter, `--help` precedence,
  `--no-cli-auto-prompt` no-op.
- **Mode resolution (Phase 2)**: env on/off/invalid, config file (`[default]` /
  `[profile X]`, `AWS_CONFIG_FILE`), env > config precedence, explicit flag >
  env, on-partial (valid = no prompt / usage error = prompt), config-driven AND
  prompt_toolkit absent -> fall-through. Tests pin `AWS_CLI_AUTO_PROMPT=off` via
  an autouse fixture in `tests/cli/conftest.py` to isolate from the dev
  machine's `~/.aws/config`, and each test overrides it with setenv/delenv.
- **prompt_toolkit adapter**: `Completion` conversion, override exclusion,
  display_meta (importorskip).
- **Import contract**: add `prompt_toolkit` to the forbidden roots. Explicitly
  inject `AWS_CLI_AUTO_PROMPT=off` into the subprocess to guarantee that the
  usage path does not stray into the prompt branch via config.

The interactive prompt loop itself is outside the scope of golden/e2e (the
harness is non-tty; aws places auto-prompt outside parity for the same reason).

## 8. Packaging

The `boto3-s3-cli[autoprompt]` extra pulls in `prompt_toolkit>=3.0` (the same
opt-in form as the `crt` extra; since the library has no UI, it depends
directly rather than delegating to a lower layer). The dev environment keeps
`prompt_toolkit` permanently installed, following aws v2's practice of always
bundling it, to enable type-checking of the ported code and the adapter tests
(missing-dep is verified with a `find_spec` stub).
