"""What a broken profile / config file does to the error reports (design/cli.md section 6).

Three aws behaviors that only show up once the config files are involved:

- a *named* profile no file declares makes botocore's scoped-config read
  raise inside aws's error renderer, which falls back to the bare report -
  the `ParamValidation` envelope disappears while the exit code stays 252;
- a file that is not valid INI replaces the run's outcome entirely with
  botocore's `Unable to parse config file` at rc 255;
- an unknown `cli_timestamp_format` in the selected profile replaces it with
  aws's `Configuration` report at rc 253.

Every expectation here was measured against the pinned aws-cli under the
`aws [options] s3 <subcommand>` -> `boto3-s3 [options] <subcommand>` mapping
(the leading blank line aws prints before each report, and the program name,
are class-1 rules of the parity normalization - design/testing.md section 9).
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path
from typing import ClassVar

import pytest

from boto3_s3_cli import cli

_USAGE_BLOCK = (
    "usage: boto3-s3 [options] <subcommand> [parameters]\n"
    "To see help text, you can run:\n"
    "\n"
    "  boto3-s3 help\n"
    "  boto3-s3 <subcommand> help\n"
)
_ENVELOPE = "An error occurred (ParamValidation): "
# The invalid-subcommand report in both renderings - the workhorse contrast of
# this file. Two blank lines: the message ends with the newline `_check_value`
# adds, and the usage block is joined on after another.
_INVALID_CHOICE = f"argument subcommand: Found invalid choice 'bogus'\n\n\n{_USAGE_BLOCK}"
_DEGRADED_INVALID_CHOICE = f"boto3-s3: [ERROR]: {_INVALID_CHOICE}"
_ENVELOPED_INVALID_CHOICE = f"boto3-s3: [ERROR]: {_ENVELOPE}{_INVALID_CHOICE}"

# The three failures the top-level pass settles (its own parse, then the
# `--query` and `--endpoint-url` resolutions), as argv + report body. The
# ordering claim of section 6 is that a bad `--profile` leaves all three
# enveloped while a bad `AWS_PROFILE` degrades all three, so both sides are
# parametrized over this one list.
_RESOLUTION_ERRORS = [
    (
        ["ls", "--output", "bad"],
        f"argument --output: Found invalid choice 'bad'\n\n\n{_USAGE_BLOCK}",
    ),
    (
        ["ls", "--query", "["],
        'Bad value for --query [: Invalid jmespath expression: Incomplete expression:\n"["\n  ^\n',
    ),
    (
        ["ls", "--endpoint-url", "bad"],
        'Bad value for --endpoint-url "bad": scheme is missing.  '
        "Must be of the form http://<hostname>/ or https://<hostname>/\n",
    ),
]


# aws's report for a `cli_timestamp_format` its timestamp-format customization
# rejects, with the value it echoes back left to `format`.
_TIMESTAMP_REPORT = (
    "boto3-s3: [ERROR]: An error occurred (Configuration): Unknown cli_timestamp_format "
    'value: {}, valid values are "wire" or "iso8601"\n'
)


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point both config files at this test's own tmp_path, neither existing yet.

    The suite-wide isolation fixture already pins `AWS_CONFIG_FILE` at a valid
    file and clears `AWS_PROFILE`; these tests need to choose both. Returns the
    config-file path so a test can write (or leave absent) whatever it needs.
    """
    path = tmp_path / "config"
    monkeypatch.setenv("AWS_CONFIG_FILE", str(path))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
    return path


class TestUndeclaredProfileDropsTheEnvelope:
    """A `--profile` / env profile no file declares degrades the 252 reports."""

    def test_invalid_subcommand_loses_the_envelope(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli.main(["--profile", "nosuch", "bogus"]) == 252
        assert capsys.readouterr().err == _DEGRADED_INVALID_CHOICE

    def test_spelling_suggestions_survive_the_degraded_report(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Only the envelope goes; the message and the usage block are the
        # normal ones, difflib's suggestion included.
        assert cli.main(["--profile", "nosuch", "lss"]) == 252
        assert capsys.readouterr().err == (
            "boto3-s3: [ERROR]: argument subcommand: Found invalid choice 'lss'\n"
            "\nMaybe you meant:\n"
            "\n  * ls\n"
            f"\n{_USAGE_BLOCK}"
        )

    def test_unknown_options_loses_the_envelope(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli.main(["--profile", "nosuch", "cp", "a", "b", "c"]) == 252
        assert capsys.readouterr().err == "boto3-s3: [ERROR]: Unknown options: c\n"

    def test_missing_subcommand_renders_two_error_records(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws's missing-subcommand message carries its own `[ERROR]` line
        # inside it, so dropping the envelope leaves two prefixed lines.
        assert cli.main(["--profile", "nosuch"]) == 252
        assert capsys.readouterr().err == (
            "boto3-s3: [ERROR]: usage: boto3-s3 [options] <subcommand> [parameters]\n"
            "boto3-s3: [ERROR]: too few arguments\n"
        )

    def test_leaf_parse_error_loses_the_envelope(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli.main(["--profile", "nosuch", "ls", "--page-size"]) == 252
        assert capsys.readouterr().err == (
            f"boto3-s3: [ERROR]: argument --page-size: expected one argument\n\n{_USAGE_BLOCK}"
        )

    def test_profile_after_the_subcommand_degrades_too(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The globals pass consumes --profile from either side, and so does
        # aws's main parser, so the position makes no difference.
        assert cli.main(["cp", "a", "b", "c", "--profile", "nosuch"]) == 252
        assert capsys.readouterr().err == "boto3-s3: [ERROR]: Unknown options: c\n"

    @pytest.mark.parametrize("env", ["AWS_PROFILE", "AWS_DEFAULT_PROFILE"])
    def test_the_env_vars_degrade_like_the_flag(
        self,
        config: Path,
        env: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv(env, "nosuch")
        assert cli.main(["bogus"]) == 252
        assert capsys.readouterr().err == _DEGRADED_INVALID_CHOICE

    def test_an_empty_env_profile_names_the_empty_profile(
        self,
        config: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # The env read is present-wins, so `AWS_PROFILE=` selects a profile
        # nothing declares (measured: aws degrades here too).
        monkeypatch.setenv("AWS_PROFILE", "")
        assert cli.main(["bogus"]) == 252
        assert capsys.readouterr().err == _DEGRADED_INVALID_CHOICE

    def test_an_explicit_default_with_no_config_file_degrades(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # botocore only skips the existence check when *nothing* named a
        # profile; `--profile default` is a name like any other.
        assert cli.main(["--profile", "default", "bogus"]) == 252
        assert capsys.readouterr().err == _DEGRADED_INVALID_CHOICE

    def test_a_nonexistent_config_file_alone_changes_nothing(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert not config.exists()
        assert cli.main(["bogus"]) == 252
        assert capsys.readouterr().err == _ENVELOPED_INVALID_CHOICE

    def test_an_undeclared_profile_never_reaches_an_rc_253_report(
        self,
        config: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # The degradation is the renderer's, so on aws it would cost the rc-253
        # reports their envelope too - but it cannot be observed there: the
        # undeclared profile makes botocore raise `ProfileNotFound` while the
        # session reads its scoped config, long before credentials or a region
        # are resolved, so the run is the bare 255 below and never the 253
        # (measured: no credentials at all plus `AWS_PROFILE=nosuch` reports
        # this, not `Unable to locate credentials`).
        monkeypatch.setenv("AWS_PROFILE", "nosuch")
        assert cli.main(["ls", "s3://bucket/p/"]) == 255
        assert capsys.readouterr().err == (
            "boto3-s3: [ERROR]: The config profile (nosuch) could not be found\n"
        )

    def test_the_degradation_does_not_leak_into_the_next_run(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The rendering decision lives in module state (aws keeps it on its
        # session, of which a process gets one). Two in-process runs must not
        # share it. The second argv fails in the preliminary scan, upstream of
        # where the dispatch re-decides the rendering, so only `_main`'s reset
        # can restore the envelope there.
        assert cli.main(["--profile", "nosuch", "bogus"]) == 252
        assert capsys.readouterr().err == _DEGRADED_INVALID_CHOICE
        assert cli.main(["--profile"]) == 252
        assert capsys.readouterr().err == (
            f"boto3-s3: [ERROR]: {_ENVELOPE}argument --profile: expected one argument\n"
            "\nusage: boto3-s3 [--profile PROFILE] [--debug]\n"
        )
        assert cli.main(["bogus"]) == 252
        assert capsys.readouterr().err == _ENVELOPED_INVALID_CHOICE


class TestDeclaredProfilesKeepTheEnvelope:
    """The envelope only goes when the profile really is undeclared."""

    @pytest.mark.parametrize(
        "argv",
        [["--profile", "good", "bogus"], ["--profile", "two words", "bogus"]],
        ids=["plain", "shell-quoted-section"],
    )
    def test_a_declared_profile_keeps_it(
        self, config: Path, argv: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        config.write_text('[profile good]\nregion = us-east-1\n[profile "two words"]\nregion = x\n')
        assert cli.main(argv) == 252
        assert capsys.readouterr().err == _ENVELOPED_INVALID_CHOICE

    def test_a_credentials_only_profile_counts_as_declared(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # botocore merges the credentials file's section names into the
        # profile map purely so this lookup succeeds.
        (config.parent / "credentials").write_text("[credsonly]\naws_access_key_id = a\n")
        assert cli.main(["--profile", "credsonly", "bogus"]) == 252
        assert capsys.readouterr().err == _ENVELOPED_INVALID_CHOICE

    def test_a_profile_prefixed_section_is_not_a_profile(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `[profilefoo]` is plain configuration, not `[profile foo]`.
        config.write_text("[profilefoo]\nregion = us-east-1\n")
        assert cli.main(["--profile", "foo", "bogus"]) == 252
        assert capsys.readouterr().err == _DEGRADED_INVALID_CHOICE

    def test_the_flag_overrides_a_bad_env_profile(
        self,
        config: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        config.write_text("[profile good]\nregion = us-east-1\n")
        monkeypatch.setenv("AWS_PROFILE", "nosuch")
        assert cli.main(["--profile", "good", "bogus"]) == 252
        assert capsys.readouterr().err == _ENVELOPED_INVALID_CHOICE

    def test_an_empty_flag_leaves_the_env_chain_in_force(
        self,
        config: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # aws binds --profile under a truthy guard, so `--profile ""` is
        # ignored and the (declared) env profile still decides.
        config.write_text("[profile good]\nregion = us-east-1\n")
        monkeypatch.setenv("AWS_PROFILE", "good")
        assert cli.main(["--profile", "", "bogus"]) == 252
        assert capsys.readouterr().err == _ENVELOPED_INVALID_CHOICE

    def test_the_config_path_expands_environment_variables(
        self,
        config: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # botocore expands `$VAR` in the path before `~`, so a config named
        # that way is found - here proved by the profile it declares being
        # seen (measured on aws, which reads the same file).
        config.write_text("[profile good]\nregion = us-east-1\n")
        monkeypatch.setenv("BOTO3_S3_TEST_CONFIG_DIR", str(config.parent))
        monkeypatch.setenv("AWS_CONFIG_FILE", "$BOTO3_S3_TEST_CONFIG_DIR/config")
        # Left unexpanded the path would name no file, the profile would look
        # undeclared, and the report would come out degraded.
        assert cli.main(["--profile", "good", "bogus"]) == 252
        assert capsys.readouterr().err == _ENVELOPED_INVALID_CHOICE


class TestWhatABadProfileCannotReach:
    """The stages that run before aws's session learns the profile."""

    def test_the_preliminary_scan_keeps_the_envelope(
        self, config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws handles this one with the entry-point chain, built without a
        # session, so no config read can reach it.
        monkeypatch.setenv("AWS_PROFILE", "nosuch")
        assert cli.main(["--profile"]) == 252
        assert capsys.readouterr().err == (
            f"boto3-s3: [ERROR]: {_ENVELOPE}argument --profile: expected one argument\n"
            "\nusage: boto3-s3 [--profile PROFILE] [--debug]\n"
        )

    def test_the_auto_prompt_conflict_keeps_the_envelope(
        self, config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("AWS_PROFILE", "nosuch")
        assert cli.main(["--cli-auto-prompt", "--no-cli-auto-prompt", "ls"]) == 252
        assert capsys.readouterr().err == (
            f"boto3-s3: [ERROR]: {_ENVELOPE}Both --cli-auto-prompt and "
            "--no-cli-auto-prompt cannot be specified at the same time.\n"
        )

    @pytest.mark.parametrize(
        ("argv", "message"), _RESOLUTION_ERRORS, ids=["globals-parse", "query", "endpoint"]
    )
    def test_the_flag_binds_after_the_top_level_pass(
        self, config: Path, argv: list[str], message: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws's `_handle_top_level_args` binds --profile after the main parse
        # and after emitting the event those resolutions hang off, so all
        # three stay enhanced under a bad --profile (measured).
        assert cli.main(["--profile", "nosuch", *argv]) == 252
        assert capsys.readouterr().err == f"boto3-s3: [ERROR]: {_ENVELOPE}{message}"

    @pytest.mark.parametrize(
        ("argv", "message"), _RESOLUTION_ERRORS, ids=["globals-parse", "query", "endpoint"]
    )
    def test_an_env_profile_reaches_them_all(
        self,
        config: Path,
        argv: list[str],
        message: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # The other half of the same ordering: botocore reads the env vars
        # while the session is being built, so nothing inside the driver
        # escapes the degradation - the very same three reports, unenveloped.
        monkeypatch.setenv("AWS_PROFILE", "nosuch")
        assert cli.main(argv) == 252
        assert capsys.readouterr().err == f"boto3-s3: [ERROR]: {message}"


class TestUnparseableConfigFile:
    """An invalid INI file replaces the run's outcome with rc 255."""

    @pytest.mark.parametrize(
        "argv",
        [["bogus"], ["cp", "a", "b", "c"], [], ["--version"], ["help"], ["ls", "s3://b/"]],
        ids=["invalid-choice", "unknown-options", "bare", "version", "help", "valid-command"],
    )
    def test_it_preempts_every_outcome(
        self, config: Path, argv: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        config.write_text("[[[broken\n")
        assert cli.main(argv) == 255
        assert capsys.readouterr().err == (
            f"boto3-s3: [ERROR]: Unable to parse config file: {config}\n"
        )

    def test_the_preliminary_scan_still_outranks_it(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws reads the config while constructing the driver, which its
        # first-pass --profile / --debug parse precedes.
        config.write_text("[[[broken\n")
        assert cli.main(["--profile"]) == 252
        assert "Unable to parse config file" not in capsys.readouterr().err

    def test_it_beats_the_auto_prompt_conflict(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config.write_text("[[[broken\n")
        assert cli.main(["--cli-auto-prompt", "--no-cli-auto-prompt", "ls"]) == 255
        assert "Unable to parse config file" in capsys.readouterr().err

    def test_it_beats_an_undeclared_profile(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config.write_text("[[[broken\n")
        assert cli.main(["--profile", "nosuch", "bogus"]) == 255
        assert capsys.readouterr().err == (
            f"boto3-s3: [ERROR]: Unable to parse config file: {config}\n"
        )

    def test_an_unsplittable_nested_block_is_a_parse_error(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # botocore parses an indented block one level deep and reports a line
        # with no `=` as a config parse failure.
        config.write_text("[default]\ns3 =\n   no_equals_here\n")
        assert cli.main(["bogus"]) == 255
        assert capsys.readouterr().err == (
            f"boto3-s3: [ERROR]: Unable to parse config file: {config}\n"
        )

    def test_a_broken_credentials_file_reports_its_own_path(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        credentials = config.parent / "credentials"
        credentials.write_text("[[[broken\n")
        assert cli.main(["bogus"]) == 255
        assert capsys.readouterr().err == (
            f"boto3-s3: [ERROR]: Unable to parse config file: {credentials}\n"
        )

    def test_the_config_file_is_reported_first_when_both_are_broken(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config.write_text("[[[broken\n")
        (config.parent / "credentials").write_text("[[[broken\n")
        assert cli.main(["bogus"]) == 255
        assert capsys.readouterr().err == (
            f"boto3-s3: [ERROR]: Unable to parse config file: {config}\n"
        )

    def test_a_directory_is_not_a_config_file(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A directory at the config path is simply "no config file" rather than
        # a parse failure (measured on aws). This pins the outcome, not the
        # route to it: `configparser.read` also declines to open a directory,
        # so the `isfile` guard and a bare existence check agree here.
        config.mkdir()
        assert cli.main(["bogus"]) == 252
        assert capsys.readouterr().err == _ENVELOPED_INVALID_CHOICE

    def test_the_reported_path_is_the_expanded_one(
        self, config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # botocore expands `$VAR` before `~` and reports what it expanded to,
        # so the message names a path the user can open (measured on aws).
        config.write_text("[[[broken\n")
        monkeypatch.setenv("BOTO3_S3_TEST_CONFIG_DIR", str(config.parent))
        # Joined with the host separator: the reported path is the expanded
        # string itself, so a "/" written here would be what Windows reports.
        monkeypatch.setenv("AWS_CONFIG_FILE", os.path.join("$BOTO3_S3_TEST_CONFIG_DIR", "config"))
        assert cli.main(["bogus"]) == 255
        assert capsys.readouterr().err == (
            f"boto3-s3: [ERROR]: Unable to parse config file: {config}\n"
        )


class TestInvalidTimestampFormat:
    """An unknown `cli_timestamp_format` ends the run at rc 253.

    aws validates the setting in the first handler of its `session-initialized`
    event, which it emits after binding `--profile` and before handing the argv
    to any command layer. Every expectation was measured against the pinned
    aws-cli.
    """

    @pytest.mark.parametrize(
        "argv",
        [
            ["ls", "s3://bucket/p/"],
            ["ls", "help"],
            ["help"],
            [],
            ["bogus"],
            ["cp", "a", "b"],
            ["ls", "--bogus"],
        ],
        ids=[
            "listing",
            "subcommand-help",
            "help",
            "bare",
            "invalid-choice",
            "cp",
            "unknown-option",
        ],
    )
    def test_it_preempts_every_command_outcome(
        self, config: Path, argv: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        config.write_text("[default]\ncli_timestamp_format = bogus\n")
        assert cli.main(argv) == 253
        assert capsys.readouterr().err == _TIMESTAMP_REPORT.format("bogus")

    def test_the_version_flag_still_wins(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `--version` is a parse-time action of the top-level pass, which runs
        # before aws emits the event this hangs off.
        config.write_text("[default]\ncli_timestamp_format = bogus\n")
        assert cli.main(["--version"]) == 0
        assert capsys.readouterr().err == ""

    def test_the_preliminary_scan_still_outranks_it(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config.write_text("[default]\ncli_timestamp_format = bogus\n")
        assert cli.main(["--profile"]) == 252
        assert "cli_timestamp_format" not in capsys.readouterr().err

    def test_the_auto_prompt_conflict_outranks_it(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config.write_text("[default]\ncli_timestamp_format = bogus\n")
        assert cli.main(["--cli-auto-prompt", "--no-cli-auto-prompt", "ls"]) == 252
        assert "cli_timestamp_format" not in capsys.readouterr().err

    def test_an_unparseable_config_outranks_it(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The setting lives in the file that cannot be read, so the parse
        # failure is all aws ever reports.
        config.write_text("[[[broken\ncli_timestamp_format = bogus\n")
        assert cli.main(["ls", "s3://bucket/p/"]) == 255
        assert capsys.readouterr().err == (
            f"boto3-s3: [ERROR]: Unable to parse config file: {config}\n"
        )

    @pytest.mark.parametrize(
        ("argv", "message"), _RESOLUTION_ERRORS, ids=["globals-parse", "query", "endpoint"]
    )
    def test_the_global_resolutions_outrank_it(
        self, config: Path, argv: list[str], message: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws hangs those on `top-level-args-parsed`, emitted one step earlier
        # than `session-initialized`, so all three keep their 252.
        config.write_text("[default]\ncli_timestamp_format = bogus\n")
        assert cli.main(argv) == 252
        assert capsys.readouterr().err == f"boto3-s3: [ERROR]: {_ENVELOPE}{message}"

    def test_the_timeout_coercion_outranks_it(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config.write_text("[default]\ncli_timestamp_format = bogus\n")
        assert cli.main(["--cli-read-timeout", "abc", "ls"]) == 255
        assert "invalid literal for int()" in capsys.readouterr().err

    @pytest.mark.parametrize("value", ["wire", "iso8601", "  wire  "])
    def test_the_accepted_values_change_nothing(
        self, config: Path, value: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # configparser strips the value, so the padded form is the plain one.
        config.write_text(f"[default]\ncli_timestamp_format ={value}\n")
        assert cli.main(["help"]) == 0
        assert capsys.readouterr().err == ""

    def test_an_absent_key_is_the_iso8601_default(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config.write_text("[default]\nregion = us-east-1\n")
        assert cli.main(["help"]) == 0
        assert capsys.readouterr().err == ""

    @pytest.mark.parametrize("value", ["", "WIRE"], ids=["empty", "wrong-case"])
    def test_the_rejected_value_is_echoed_verbatim(
        self, config: Path, value: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # An empty value is a value, not an absent key, and the comparison is
        # case-sensitive (both measured).
        config.write_text(f"[default]\ncli_timestamp_format = {value}\n")
        assert cli.main(["help"]) == 253
        assert capsys.readouterr().err == _TIMESTAMP_REPORT.format(value)

    def test_the_key_is_case_insensitive(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # configparser lowercases option names, as botocore's parse does.
        config.write_text("[default]\nCLI_TIMESTAMP_FORMAT = bogus\n")
        assert cli.main(["help"]) == 253
        assert capsys.readouterr().err == _TIMESTAMP_REPORT.format("bogus")

    def test_an_indented_block_is_reported_as_the_map_botocore_builds(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # botocore parses an indented `key = value` block into a dict, and the
        # report interpolates whatever it parsed (measured).
        config.write_text("[default]\ncli_timestamp_format =\n  wire = x\n")
        assert cli.main(["help"]) == 253
        assert capsys.readouterr().err == _TIMESTAMP_REPORT.format("{'wire': 'x'}")


class TestWhichProfileTheTimestampFormatComesFrom:
    """The setting is read from the scoped config of the profile aws bound."""

    def test_an_unselected_profiles_value_is_ignored(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config.write_text("[default]\n[profile p]\ncli_timestamp_format = bogus\n")
        assert cli.main(["help"]) == 0
        assert capsys.readouterr().err == ""

    def test_the_flag_selects_it(self, config: Path, capsys: pytest.CaptureFixture[str]) -> None:
        config.write_text("[default]\n[profile p]\ncli_timestamp_format = bogus\n")
        assert cli.main(["--profile", "p", "help"]) == 253
        assert capsys.readouterr().err == _TIMESTAMP_REPORT.format("bogus")

    @pytest.mark.parametrize("env", ["AWS_PROFILE", "AWS_DEFAULT_PROFILE"])
    def test_the_env_vars_select_it(
        self,
        config: Path,
        env: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        config.write_text("[default]\n[profile p]\ncli_timestamp_format = bogus\n")
        monkeypatch.setenv(env, "p")
        assert cli.main(["help"]) == 253
        assert capsys.readouterr().err == _TIMESTAMP_REPORT.format("bogus")

    def test_a_declared_flag_profile_restores_the_envelope_first(
        self,
        config: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # The gate runs *after* --profile re-decides the rendering, so a
        # declared flag profile lifts the degradation an undeclared env profile
        # imposed and the report comes out enveloped (measured: aws is rc 253
        # `An error occurred (Configuration): ...` for this exact combination).
        config.write_text("[default]\n[profile p]\ncli_timestamp_format = bogus\n")
        monkeypatch.setenv("AWS_PROFILE", "nosuch")
        assert cli.main(["--profile", "p", "ls", "help"]) == 253
        assert capsys.readouterr().err == _TIMESTAMP_REPORT.format("bogus")

    def test_a_selected_clean_profile_shadows_the_default_section(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws's scoped config is the one profile's options, not a merge with
        # `[default]`, so selecting a clean profile silences the setting.
        config.write_text("[default]\ncli_timestamp_format = bogus\n[profile p]\n")
        assert cli.main(["--profile", "p", "help"]) == 0
        assert capsys.readouterr().err == ""

    def test_an_empty_flag_leaves_the_env_chain_in_force(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws's truthy guard again: `--profile ""` binds nothing, so the
        # default section still decides.
        config.write_text("[default]\ncli_timestamp_format = bogus\n")
        assert cli.main(["--profile", "", "help"]) == 253
        assert capsys.readouterr().err == _TIMESTAMP_REPORT.format("bogus")

    @pytest.mark.parametrize("argv", [["--profile", "nosuch", "help"], ["help"]])
    def test_an_undeclared_profile_falls_back_to_the_default_format(
        self,
        config: Path,
        argv: list[str],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # botocore raises `ProfileNotFound` for the scoped-config read and aws's
        # handler catches exactly that, keeping its `iso8601` default - so the
        # `[default]` section's bad value is never seen (measured). This is also
        # why an rc-253 `Configuration` report can never come out degraded: the
        # undeclared profile that would strip the envelope also stands the check
        # down.
        config.write_text("[default]\ncli_timestamp_format = bogus\n")
        if argv == ["help"]:
            monkeypatch.setenv("AWS_PROFILE", "nosuch")
        assert cli.main(argv) == 0
        assert capsys.readouterr().err == ""

    def test_the_credentials_file_is_part_of_the_scoped_config(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # botocore merges the credentials file into the profile map, so a
        # setting written there is read like any other.
        config.write_text("[default]\n")
        (config.parent / "credentials").write_text("[default]\ncli_timestamp_format = bogus\n")
        assert cli.main(["help"]) == 253
        assert capsys.readouterr().err == _TIMESTAMP_REPORT.format("bogus")

    def test_the_credentials_file_wins_over_the_config_file(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The merge is per key, credentials last (botocore's `full_config`).
        config.write_text("[default]\ncli_timestamp_format = wire\n")
        (config.parent / "credentials").write_text("[default]\ncli_timestamp_format = bogus\n")
        assert cli.main(["help"]) == 253
        assert capsys.readouterr().err == _TIMESTAMP_REPORT.format("bogus")

    def test_the_credentials_file_can_repair_the_config_file(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config.write_text("[default]\ncli_timestamp_format = bogus\n")
        (config.parent / "credentials").write_text("[default]\ncli_timestamp_format = wire\n")
        assert cli.main(["help"]) == 0
        assert capsys.readouterr().err == ""

    def test_a_credentials_section_updates_rather_than_replaces(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The other half of "per key": a credentials section that carries some
        # *other* key leaves the config file's setting standing, where a
        # wholesale replacement would drop it (measured: aws is still rc 253).
        config.write_text("[default]\ncli_timestamp_format = bogus\n")
        (config.parent / "credentials").write_text("[default]\nregion = us-west-2\n")
        assert cli.main(["help"]) == 253
        assert capsys.readouterr().err == _TIMESTAMP_REPORT.format("bogus")

    @pytest.mark.parametrize(
        "text",
        [
            "[default]\ncli_timestamp_format = bogus\n[profile default]\nregion = us-east-1\n",
            "[profile default]\ncli_timestamp_format = bogus\n[default]\nregion = us-east-1\n",
        ],
        ids=["default-first", "profile-default-first"],
    )
    def test_a_later_section_claims_the_profile_outright(
        self, config: Path, text: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `[default]` and `[profile default]` name the same profile; botocore's
        # loop assigns whole sections, so the later one replaces the earlier
        # rather than merging into it (measured both ways round).
        config.write_text(text)
        assert cli.main(["help"]) == 0
        assert capsys.readouterr().err == ""


class TestUnknownOutputEncoding:
    """An unknown `AWS_CLI_OUTPUT_ENCODING` codec ends the run at rc 255.

    aws validates the variable between handling the top-level args and
    emitting `session-initialized` (`validate_preferred_output_encoding`), so
    the report is its general handler's - no envelope - and it beats both
    config gates and every command layer while the top-level resolutions
    still win. Every expectation was measured against the pinned aws-cli.
    """

    _REPORT = "boto3-s3: [ERROR]: Unknown codec `{}` specified for AWS_CLI_OUTPUT_ENCODING.\n"

    @pytest.mark.parametrize(
        "argv",
        [
            ["ls", "s3://bucket/p/"],
            ["help"],
            ["bogus"],
            ["ls", "--bogus"],
            ["rm", "s3://b/k", "--dryrun"],
        ],
        ids=["listing", "help", "invalid-choice", "unknown-option", "rm-dryrun"],
    )
    def test_it_preempts_every_command_outcome(
        self, argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("AWS_CLI_OUTPUT_ENCODING", "nosuch")
        assert cli.main(argv) == 255
        assert capsys.readouterr().err == self._REPORT.format("nosuch")

    def test_it_preempts_both_config_gates(
        self, config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws's check runs before `session-initialized`, which both config
        # gates hang off (measured: the codec report wins over a broken
        # `cli_timestamp_format` and a broken `cli_binary_format`).
        config.write_text("[default]\ncli_timestamp_format = bogus\ncli_binary_format = bogus\n")
        monkeypatch.setenv("AWS_CLI_OUTPUT_ENCODING", "nosuch")
        assert cli.main(["ls", "s3://bucket/p/"]) == 255
        assert capsys.readouterr().err == self._REPORT.format("nosuch")

    def test_an_empty_value_is_an_unknown_codec(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws tests `in os.environ`, not truthiness: the empty string reaches
        # the codec lookup and fails it (measured).
        monkeypatch.setenv("AWS_CLI_OUTPUT_ENCODING", "")
        assert cli.main(["help"]) == 255
        assert capsys.readouterr().err == self._REPORT.format("")

    def test_a_valid_codec_leaves_a_clean_run_alone(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The gate rejects nothing a codec lookup accepts; what a valid one
        # then does to the *reports* is `TestOutputEncodingIsApplied`.
        monkeypatch.setenv("AWS_CLI_OUTPUT_ENCODING", "utf-8")
        assert cli.main(["help"]) == 0
        assert capsys.readouterr().err == ""

    def test_the_version_flag_still_wins(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("AWS_CLI_OUTPUT_ENCODING", "nosuch")
        assert cli.main(["--version"]) == 0
        assert capsys.readouterr().err == ""

    def test_the_preliminary_scan_still_outranks_it(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("AWS_CLI_OUTPUT_ENCODING", "nosuch")
        assert cli.main(["--profile"]) == 252
        assert "Unknown codec" not in capsys.readouterr().err

    def test_the_auto_prompt_conflict_outranks_it(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("AWS_CLI_OUTPUT_ENCODING", "nosuch")
        assert cli.main(["--cli-auto-prompt", "--no-cli-auto-prompt", "ls"]) == 252
        assert "Unknown codec" not in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("argv", "message"), _RESOLUTION_ERRORS, ids=["globals-parse", "query", "endpoint"]
    )
    def test_the_global_resolutions_outrank_it(
        self,
        argv: list[str],
        message: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("AWS_CLI_OUTPUT_ENCODING", "nosuch")
        assert cli.main(argv) == 252
        assert capsys.readouterr().err == f"boto3-s3: [ERROR]: {_ENVELOPE}{message}"

    def test_the_timeout_coercion_outranks_it(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("AWS_CLI_OUTPUT_ENCODING", "nosuch")
        assert cli.main(["--cli-read-timeout", "abc", "ls"]) == 255
        assert "invalid literal for int()" in capsys.readouterr().err

    def test_an_unparseable_config_outranks_it(
        self, config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config.write_text("[[[broken\n")
        monkeypatch.setenv("AWS_CLI_OUTPUT_ENCODING", "nosuch")
        assert cli.main(["ls", "s3://bucket/p/"]) == 255
        assert capsys.readouterr().err == (
            f"boto3-s3: [ERROR]: Unable to parse config file: {config}\n"
        )


class TestOutputEncodingIsApplied:
    """A valid `AWS_CLI_OUTPUT_ENCODING` re-encodes the error reports.

    aws builds a text writer for stdout and one for stderr every time it
    reports an error, and building one reconfigures the stream to the codec
    (`compat.set_preferred_output_encoding`). So the bytes of a report change,
    a stateful codec gets to add its BOM, and - because reconfiguring resets
    the error handler to `strict` - a report the codec cannot represent fails
    to write, escapes to aws's entry-point chain and is replaced by the codec
    error at rc 255. Every expectation below was measured against the pinned
    aws-cli, byte for byte under the program-name mapping (the character
    position a codec error names shifts by the width of that name, and aws's
    leading blank line - class-1 rules of the normalization,
    design/testing.md section 9).

    Nothing else re-encodes: a successful run's result, progress and warning
    lines are the interpreter's own on both tools (measured).
    """

    # An unknown option carrying U+00E9: the shortest report with a non-ASCII
    # character in it that needs no client and no filesystem.
    _ARGV: ClassVar[list[str]] = ["ls", "--café"]
    _REPORT = "boto3-s3: [ERROR]: An error occurred (ParamValidation): Unknown options: --café\n"

    def _stderr_bytes(self, monkeypatch: pytest.MonkeyPatch, encoding: str = "utf-8") -> io.BytesIO:
        """Swap `sys.stderr` for a real text stream over a byte buffer.

        capsys decodes what it captured as UTF-8, which cannot show what a
        re-encoded report actually put on the wire - and a `cp1252` report is
        not valid UTF-8 at all. The replacement is a `TextIOWrapper` because
        that is the one thing the codec application needs: a stream it can
        reconfigure.
        """
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding=encoding, newline="", write_through=True)
        monkeypatch.setattr(sys, "stderr", stream)
        return raw

    def test_the_report_is_written_in_the_codec(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # cp1252 spells U+00E9 as the single byte 0xe9 where the stream's own
        # UTF-8 spells it 0xc3 0xa9 (measured on the pinned aws-cli).
        raw = self._stderr_bytes(monkeypatch)
        monkeypatch.setenv("AWS_CLI_OUTPUT_ENCODING", "cp1252")
        assert cli.main(self._ARGV) == 252
        assert raw.getvalue() == self._REPORT.encode("cp1252")

    @pytest.mark.parametrize("codec", ["utf_8_sig", "utf-16"])
    def test_a_stateful_codec_gets_its_byte_order_mark(
        self, codec: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raw = self._stderr_bytes(monkeypatch)
        monkeypatch.setenv("AWS_CLI_OUTPUT_ENCODING", codec)
        assert cli.main(self._ARGV) == 252
        assert raw.getvalue() == self._REPORT.encode(codec)

    def test_a_codec_that_cannot_write_it_turns_252_into_255(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The strict handler makes the write raise: aws's renderer drops the
        # enveloped attempt, retries bare, and lets *that* failure reach its
        # entry-point chain - which is why the position named below is the
        # bare message's and the exit code is the general handler's, not the
        # 252 the run had earned.
        raw = self._stderr_bytes(monkeypatch)
        monkeypatch.setenv("AWS_CLI_OUTPUT_ENCODING", "ascii")
        assert cli.main(self._ARGV) == 255
        assert raw.getvalue() == (
            b"boto3-s3: [ERROR]: 'ascii' codec can't encode character '\\xe9' "
            b"in position 41: ordinal not in range(128)\n"
        )

    def test_a_codec_that_swallows_it_reports_nothing_at_255(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `idna` rejects a label longer than 63 characters and buffers any
        # trailing label that has no dot to close it, so the report raises and
        # the codec error that replaces it is silently buffered away: rc 255
        # with an empty stderr, exactly as aws ends this run.
        raw = self._stderr_bytes(monkeypatch)
        monkeypatch.setenv("AWS_CLI_OUTPUT_ENCODING", "idna")
        assert cli.main(["ls", f"--{'a' * 70}.x"]) == 255
        assert raw.getvalue() == b""

    def test_pythonutf8_is_the_fallback_codec(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # aws still honors PYTHONUTF8=1 for the report streams (its frozen
        # interpreter ignores it everywhere else), so the report goes out as
        # UTF-8 even where the stream itself is not.
        raw = self._stderr_bytes(monkeypatch, "latin-1")
        monkeypatch.delenv("AWS_CLI_OUTPUT_ENCODING", raising=False)
        monkeypatch.setenv("PYTHONUTF8", "1")
        assert cli.main(self._ARGV) == 252
        assert raw.getvalue() == self._REPORT.encode("utf-8")

    def test_without_either_variable_the_stream_is_left_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The contrast for both branches above: an untouched stream writes the
        # report in its own encoding.
        raw = self._stderr_bytes(monkeypatch, "latin-1")
        monkeypatch.delenv("AWS_CLI_OUTPUT_ENCODING", raising=False)
        monkeypatch.delenv("PYTHONUTF8", raising=False)
        assert cli.main(self._ARGV) == 252
        assert raw.getvalue() == self._REPORT.encode("latin-1")

    def test_a_run_that_reports_nothing_never_re_encodes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The application point is the report itself, so a run that makes none
        # leaves both streams as the interpreter set them up (measured: a
        # narrowed codec changes no byte of a successful command).
        self._stderr_bytes(monkeypatch)
        monkeypatch.setenv("AWS_CLI_OUTPUT_ENCODING", "cp1252")
        assert cli.main(["help"]) == 0
        assert sys.stderr.encoding == "utf-8"


class TestInvalidBinaryFormat:
    """An unknown `cli_binary_format` in the selected profile ends the run at rc 255.

    aws resolves the setting in its `session-initialized` binary-format
    customization, whose handler table the value indexes directly - the report
    is the bare KeyError repr through the general handler, after the timestamp
    gate and ahead of every command layer. Every expectation was measured
    against the pinned aws-cli.
    """

    @pytest.mark.parametrize(
        "argv",
        [
            ["ls", "s3://bucket/p/"],
            ["help"],
            ["ls", "help"],
            ["bogus"],
            ["cp", "--badopt"],
        ],
        ids=["listing", "help", "subcommand-help", "invalid-choice", "unknown-option"],
    )
    def test_it_preempts_every_command_outcome(
        self, config: Path, argv: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        config.write_text("[default]\ncli_binary_format = bogus\n")
        assert cli.main(argv) == 255
        assert capsys.readouterr().err == "boto3-s3: [ERROR]: 'bogus'\n"

    def test_the_timestamp_gate_outranks_it(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws registers the timestamp customization first on
        # `session-initialized`, so with both broken the timestamp report is
        # the run's outcome (measured: rc 253).
        config.write_text("[default]\ncli_timestamp_format = nope\ncli_binary_format = bogus\n")
        assert cli.main(["ls", "s3://bucket/p/"]) == 253
        assert capsys.readouterr().err == _TIMESTAMP_REPORT.format("nope")

    def test_an_explicit_flag_never_consults_the_config(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws reads `parsed_args.cli_binary_format` first; argparse already
        # restricted the flag to the valid choices, so a broken config value
        # is simply never read (measured: the run proceeds).
        config.write_text("[default]\ncli_binary_format = bogus\n")
        assert cli.main(["--cli-binary-format", "base64", "help"]) == 0
        assert capsys.readouterr().err == ""

    @pytest.mark.parametrize("value", ["base64", "raw-in-base64-out"])
    def test_the_accepted_values_change_nothing(
        self, config: Path, value: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config.write_text(f"[default]\ncli_binary_format = {value}\n")
        assert cli.main(["help"]) == 0
        assert capsys.readouterr().err == ""

    @pytest.mark.parametrize("value", ["", "Base64"], ids=["empty", "wrong-case"])
    def test_the_rejected_value_renders_as_its_repr(
        self, config: Path, value: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # An empty value is a value, and the table lookup is case-sensitive
        # (both measured: aws prints `''` and `'Base64'`).
        config.write_text(f"[default]\ncli_binary_format = {value}\n")
        assert cli.main(["help"]) == 255
        assert capsys.readouterr().err == f"boto3-s3: [ERROR]: {value!r}\n"

    def test_an_indented_block_is_the_unhashable_key_report(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # botocore parses the block into a map, which aws's table lookup
        # rejects as an unhashable dict key - its official build's (Python
        # 3.14) TypeError text, pinned across hosts (measured).
        config.write_text("[default]\ncli_binary_format =\n  b = x\n")
        assert cli.main(["help"]) == 255
        assert capsys.readouterr().err == (
            "boto3-s3: [ERROR]: cannot use 'dict' as a dict key (unhashable type: 'dict')\n"
        )

    def test_an_undeclared_profile_stands_the_gate_down(
        self, config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws's handler catches `ProfileNotFound` and keeps its `base64`
        # default, so the run proceeds to whatever the command decides
        # (measured: `help` pages at rc 0).
        config.write_text("[default]\ncli_binary_format = bogus\n")
        monkeypatch.setenv("AWS_PROFILE", "nosuch")
        assert cli.main(["help"]) == 0
        assert capsys.readouterr().err == ""

    def test_an_unselected_profiles_value_is_ignored(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config.write_text("[default]\n[profile p]\ncli_binary_format = bogus\n")
        assert cli.main(["help"]) == 0
        assert capsys.readouterr().err == ""

    def test_the_flag_selects_the_profile(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config.write_text("[default]\n[profile p]\ncli_binary_format = bogus\n")
        assert cli.main(["--profile", "p", "help"]) == 255
        assert capsys.readouterr().err == "boto3-s3: [ERROR]: 'bogus'\n"

    def test_the_version_flag_still_wins(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config.write_text("[default]\ncli_binary_format = bogus\n")
        assert cli.main(["--version"]) == 0
        assert capsys.readouterr().err == ""


# Every value aws's error-format choice list accepts.
_ERROR_FORMATS = ["enhanced", "legacy", "json", "yaml", "text", "table"]


class TestTheErrorFormatIsAcceptedAndIgnored:
    """`--cli-error-format` / `AWS_CLI_ERROR_FORMAT` change no report here.

    aws acts on them - `legacy` strips the envelope, `enhanced` restores it
    where an undeclared profile dropped it, and `json` / `yaml` / `text` /
    `table` re-render the report altogether - while this command validates the
    value and does nothing with it. The divergence is deliberate and recorded
    (docs/cli/aws-differences.md sections 1 and 3,
    design/aws-cli-option-handling.md section 2); these rows pin that the
    rendering really is value-independent on both spellings, so the record
    cannot silently go stale.
    """

    @pytest.mark.parametrize("value", _ERROR_FORMATS)
    def test_the_flag_leaves_an_enveloped_report_alone(
        self, config: Path, value: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli.main(["--cli-error-format", value, "bogus"]) == 252
        assert capsys.readouterr().err == _ENVELOPED_INVALID_CHOICE

    @pytest.mark.parametrize("value", _ERROR_FORMATS)
    def test_the_env_var_leaves_an_enveloped_report_alone(
        self,
        config: Path,
        value: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("AWS_CLI_ERROR_FORMAT", value)
        assert cli.main(["bogus"]) == 252
        assert capsys.readouterr().err == _ENVELOPED_INVALID_CHOICE

    @pytest.mark.parametrize("value", _ERROR_FORMATS)
    def test_it_never_restores_a_dropped_envelope(
        self, config: Path, value: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws's `enhanced` puts the envelope back in exactly this situation
        # (measured on the pinned aws-cli); here the report stays degraded
        # whatever the value.
        assert cli.main(["--profile", "nosuch", "--cli-error-format", value, "bogus"]) == 252
        assert capsys.readouterr().err == _DEGRADED_INVALID_CHOICE

    def test_an_unknown_value_is_still_rejected(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Ignoring the value does not mean skipping its choice list.
        assert cli.main(["--cli-error-format", "bogus", "ls"]) == 252
        assert (
            "argument --cli-error-format: Found invalid choice 'bogus'" in capsys.readouterr().err
        )


class TestCliHistoryIsNotRead:
    """`cli_history` reaches nothing here (docs/cli/aws-differences.md section 2).

    aws records the run in a history database when the selected profile
    enables it, and prints `Warning: Unable to record CLI history. ...` on one
    stderr line when it cannot open one, ahead of the command's own output and
    without touching the exit code (both measured on the pinned aws-cli).
    This command has no history mechanism, so every value is inert.
    """

    @pytest.mark.parametrize("value", ["enabled", "disabled", "bogus"])
    def test_no_value_changes_the_run(
        self, config: Path, value: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config.write_text(f"[default]\ncli_history = {value}\n")
        assert cli.main(["help"]) == 0
        assert capsys.readouterr().err == ""

    def test_it_adds_no_warning_to_a_failing_run(
        self, config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws's warning rides on an unopenable database, so point the variable
        # at a directory - the shape that makes aws warn - and pin that the
        # report is the run's own and nothing else.
        config.write_text("[default]\ncli_history = enabled\n")
        monkeypatch.setenv("AWS_CLI_HISTORY_FILE", str(config.parent))
        assert cli.main(["bogus"]) == 252
        assert capsys.readouterr().err == _ENVELOPED_INVALID_CHOICE
