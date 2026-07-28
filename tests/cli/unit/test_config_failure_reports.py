"""What a broken profile / config file does to the error reports (design/cli.md section 6).

Two aws behaviors that only show up once the config files are involved:

- a *named* profile no file declares makes botocore's scoped-config read
  raise inside aws's error renderer, which falls back to the bare report -
  the `ParamValidation` envelope disappears while the exit code stays 252;
- a file that is not valid INI replaces the run's outcome entirely with
  botocore's `Unable to parse config file` at rc 255.

Every expectation here was measured against the pinned aws-cli under the
`aws [options] s3 <subcommand>` -> `boto3-s3 [options] <subcommand>` mapping
(the leading blank line aws prints before each report, and the program name,
are the known residuals - `aws-cli-option-handling.md` section 6).
"""

from __future__ import annotations

from pathlib import Path

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
        monkeypatch.setenv("AWS_CONFIG_FILE", "$BOTO3_S3_TEST_CONFIG_DIR/config")
        assert cli.main(["bogus"]) == 255
        assert capsys.readouterr().err == (
            f"boto3-s3: [ERROR]: Unable to parse config file: {config}\n"
        )
