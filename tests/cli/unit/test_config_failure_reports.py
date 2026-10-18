"""What the config files do to a run (design/cli.md section 6).

botocore reads aws's config and credentials files when a command opens its
session, and that read is where a broken setting stops the run:

- a file that is not valid INI fails the command with botocore's
  `Unable to parse config file` at rc 255;
- an unknown `cli_timestamp_format` in the selected profile fails it at
  rc 253, an unknown `cli_binary_format` at rc 255;
- an IMDS setting botocore refuses while it builds the credential chain fails
  it at rc 255, with botocore's own text.

`help` and `--version` open no session, so none of these reach them. The rest
of the file pins how `AWS_CLI_OUTPUT_ENCODING`, `--cli-error-format` and
`cli_history` act on the reports.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import ClassVar

import pytest

from boto3_s3_cli import cli


def _invalid_subcommand_report(capsys: pytest.CaptureFixture[str]) -> str:
    """The report a bare invalid subcommand gets under the current settings."""
    assert cli.main(["bogus"]) == 252
    return capsys.readouterr().err


# Commands that open a session: two that succeed without a server (so a
# setting that is accepted shows as rc 0) and a listing.
_SESSION_COMMANDS = [
    ["presign", "s3://bucket/key"],
    ["rm", "s3://bucket/key", "--dryrun"],
    ["ls", "s3://bucket/p/"],
]
_SESSION_COMMAND_IDS = ["presign", "rm-dryrun", "ls"]
# The one used where any session-opening command will do.
_PRESIGN = ["presign", "s3://bucket/key"]

_TIMESTAMP_REPORT = (
    "boto3-s3: [ERROR]: Unknown cli_timestamp_format value: {}, "
    'valid values are "wire" or "iso8601"\n'
)
_BINARY_REPORT = (
    "boto3-s3: [ERROR]: Unknown cli_binary_format value: {}, "
    'valid values are "base64" or "raw-in-base64-out"\n'
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


class TestUnparseableConfigFile:
    """An invalid INI file fails every command at rc 255, with botocore's report."""

    @pytest.mark.parametrize("argv", _SESSION_COMMANDS, ids=_SESSION_COMMAND_IDS)
    def test_it_fails_every_command(
        self, config: Path, argv: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        config.write_text("[[[broken\n")
        assert cli.main(argv) == 255
        assert capsys.readouterr().err == (
            f"boto3-s3: [ERROR]: Unable to parse config file: {config}\n"
        )

    def test_a_broken_credentials_file_reports_its_own_path(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        credentials = config.parent / "credentials"
        credentials.write_text("[[[broken\n")
        assert cli.main(_PRESIGN) == 255
        assert capsys.readouterr().err == (
            f"boto3-s3: [ERROR]: Unable to parse config file: {credentials}\n"
        )

    def test_an_unsplittable_nested_block_is_a_parse_error(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # botocore parses an indented block one level deep and reports a line
        # with no `=` as a config parse failure.
        config.write_text("[default]\ns3 =\n   no_equals_here\n")
        assert cli.main(_PRESIGN) == 255
        assert capsys.readouterr().err == (
            f"boto3-s3: [ERROR]: Unable to parse config file: {config}\n"
        )

    @pytest.mark.parametrize(
        "text",
        [
            "[[[broken\n",
            "[default]\ncli_timestamp_format = bogus\n",
            "[default]\ncli_binary_format = bogus\n",
        ],
        ids=["unparseable", "timestamp-format", "binary-format"],
    )
    @pytest.mark.parametrize("argv", [["help"], ["--version"]], ids=["help", "version"])
    def test_the_informational_exits_do_not_read_it(
        self, config: Path, text: str, argv: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        # They open no session, so a broken config file never reaches them
        # (aws reads the config first and fails them too; the difference is
        # accepted, design/cli.md section 6).
        config.write_text(text)
        assert cli.main(argv) == 0
        assert capsys.readouterr().err == ""


class TestInvalidTimestampFormat:
    """An unknown `cli_timestamp_format` fails every command at rc 253."""

    @pytest.mark.parametrize("argv", _SESSION_COMMANDS, ids=_SESSION_COMMAND_IDS)
    def test_it_fails_every_command(
        self, config: Path, argv: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        config.write_text("[default]\ncli_timestamp_format = bogus\n")
        assert cli.main(argv) == 253
        assert capsys.readouterr().err == _TIMESTAMP_REPORT.format("bogus")

    @pytest.mark.parametrize("value", ["wire", "iso8601", "  wire  "])
    def test_the_accepted_values_change_nothing(
        self, config: Path, value: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # configparser strips the value, so the padded form is the plain one.
        config.write_text(f"[default]\ncli_timestamp_format ={value}\n")
        assert cli.main(_PRESIGN) == 0
        assert capsys.readouterr().err == ""

    def test_an_absent_key_is_the_iso8601_default(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config.write_text("[default]\nregion = us-east-1\n")
        assert cli.main(_PRESIGN) == 0
        assert capsys.readouterr().err == ""

    @pytest.mark.parametrize("value", ["", "WIRE"], ids=["empty", "wrong-case"])
    def test_the_rejected_value_is_echoed_verbatim(
        self, config: Path, value: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # An empty value is a value, not an absent key, and the comparison is
        # case-sensitive (both measured on aws).
        config.write_text(f"[default]\ncli_timestamp_format = {value}\n")
        assert cli.main(_PRESIGN) == 253
        assert capsys.readouterr().err == _TIMESTAMP_REPORT.format(value)

    def test_the_key_is_case_insensitive(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # configparser lowercases option names, as botocore's parse does.
        config.write_text("[default]\nCLI_TIMESTAMP_FORMAT = bogus\n")
        assert cli.main(_PRESIGN) == 253
        assert capsys.readouterr().err == _TIMESTAMP_REPORT.format("bogus")


class TestWhichProfileTheFormatsComeFrom:
    """Both settings are read from the scoped config of the selected profile."""

    def test_an_unselected_profiles_value_is_ignored(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config.write_text(
            "[default]\n[profile p]\ncli_timestamp_format = bogus\ncli_binary_format = bogus\n"
        )
        assert cli.main(_PRESIGN) == 0
        assert capsys.readouterr().err == ""

    def test_the_flag_selects_it(self, config: Path, capsys: pytest.CaptureFixture[str]) -> None:
        config.write_text("[default]\n[profile p]\ncli_timestamp_format = bogus\n")
        assert cli.main(["--profile", "p", *_PRESIGN]) == 253
        assert capsys.readouterr().err == _TIMESTAMP_REPORT.format("bogus")

    @pytest.mark.parametrize("env", ["AWS_PROFILE", "AWS_DEFAULT_PROFILE"])
    def test_the_env_vars_select_it(
        self,
        config: Path,
        env: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        config.write_text("[default]\n[profile p]\ncli_binary_format = bogus\n")
        monkeypatch.setenv(env, "p")
        assert cli.main(_PRESIGN) == 255
        assert capsys.readouterr().err == _BINARY_REPORT.format("bogus")

    def test_a_selected_clean_profile_shadows_the_default_section(
        self, config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The scoped config is the one profile's options, not a merge with
        # `[default]`, so selecting a clean profile silences the setting. The
        # environment selects it: a `--profile` would also turn off the
        # environment credentials the presign signs with.
        config.write_text("[default]\ncli_timestamp_format = bogus\n[profile p]\n")
        monkeypatch.setenv("AWS_PROFILE", "p")
        assert cli.main(_PRESIGN) == 0
        assert capsys.readouterr().err == ""

    def test_an_empty_flag_leaves_the_env_chain_in_force(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws's truthy guard: `--profile ""` binds nothing, so the default
        # section still decides.
        config.write_text("[default]\ncli_timestamp_format = bogus\n")
        assert cli.main(["--profile", "", *_PRESIGN]) == 253
        assert capsys.readouterr().err == _TIMESTAMP_REPORT.format("bogus")

    def test_an_undeclared_profile_is_reported_as_itself(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The format check stands down on botocore's `ProfileNotFound`, as
        # aws's handlers do, and the session's other reads report the profile.
        config.write_text("[default]\ncli_timestamp_format = bogus\n")
        assert cli.main(["--profile", "nosuch", *_PRESIGN]) == 255
        assert capsys.readouterr().err == (
            "boto3-s3: [ERROR]: The config profile (nosuch) could not be found\n"
        )

    def test_the_credentials_file_is_part_of_the_scoped_config(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # botocore merges the credentials file into the profile map, so a
        # setting written there is read like any other.
        config.write_text("[default]\n")
        (config.parent / "credentials").write_text("[default]\ncli_timestamp_format = bogus\n")
        assert cli.main(_PRESIGN) == 253
        assert capsys.readouterr().err == _TIMESTAMP_REPORT.format("bogus")


class TestInvalidBinaryFormat:
    """An unknown `cli_binary_format` fails every command at rc 255."""

    @pytest.mark.parametrize("argv", _SESSION_COMMANDS, ids=_SESSION_COMMAND_IDS)
    def test_it_fails_every_command(
        self, config: Path, argv: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        config.write_text("[default]\ncli_binary_format = bogus\n")
        assert cli.main(argv) == 255
        assert capsys.readouterr().err == _BINARY_REPORT.format("bogus")

    def test_an_explicit_flag_never_consults_the_config(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws reads `--cli-binary-format` first; argparse already restricted
        # the flag to the valid choices, so a broken config value is never
        # read (measured on aws: the run proceeds).
        config.write_text("[default]\ncli_binary_format = bogus\n")
        assert cli.main(["--cli-binary-format", "base64", *_PRESIGN]) == 0
        assert capsys.readouterr().err == ""

    @pytest.mark.parametrize("value", ["base64", "raw-in-base64-out"])
    def test_the_accepted_values_change_nothing(
        self, config: Path, value: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config.write_text(f"[default]\ncli_binary_format = {value}\n")
        assert cli.main(_PRESIGN) == 0
        assert capsys.readouterr().err == ""

    @pytest.mark.parametrize("value", ["", "Base64"], ids=["empty", "wrong-case"])
    def test_an_empty_or_wrong_case_value_is_refused(
        self, config: Path, value: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # An empty value is a value, and aws's lookup is case-sensitive (both
        # measured on aws: rc 255).
        config.write_text(f"[default]\ncli_binary_format = {value}\n")
        assert cli.main(_PRESIGN) == 255
        assert capsys.readouterr().err == _BINARY_REPORT.format(value)


class TestUnknownOutputEncoding:
    """An unknown `AWS_CLI_OUTPUT_ENCODING` codec ends the run at rc 255.

    aws validates the variable between handling the top-level args and
    emitting `session-initialized` (`validate_preferred_output_encoding`), so
    the report is its general handler's - no envelope. Every expectation was
    measured against the pinned aws-cli.
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

    def test_it_preempts_both_format_checks(
        self, config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The codec is checked before any command opens its session, where
        # the two format settings are checked (aws reports the codec too).
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

    def test_a_top_level_parse_failure_is_written_the_same_way(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A global that fails to parse is reported by the same writer as
        # every other report, so the same drop-the-envelope retry applies.
        raw = self._stderr_bytes(monkeypatch)
        monkeypatch.setenv("AWS_CLI_OUTPUT_ENCODING", "ascii")
        assert cli.main(["ls", "--output", "b\u00e9d"]) == 255
        assert raw.getvalue().startswith(
            b"boto3-s3: [ERROR]: 'ascii' codec can't encode character '\\xe9' "
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
        expected = _invalid_subcommand_report(capsys)
        assert cli.main(["--cli-error-format", value, "bogus"]) == 252
        assert capsys.readouterr().err == expected

    @pytest.mark.parametrize("value", _ERROR_FORMATS)
    def test_the_env_var_leaves_an_enveloped_report_alone(
        self,
        config: Path,
        value: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        expected = _invalid_subcommand_report(capsys)
        monkeypatch.setenv("AWS_CLI_ERROR_FORMAT", value)
        assert cli.main(["bogus"]) == 252
        assert capsys.readouterr().err == expected

    def test_an_unknown_value_is_still_rejected(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Ignoring the value does not mean skipping its choice list.
        assert cli.main(["--cli-error-format", "bogus", "ls"]) == 252
        assert "argument --cli-error-format: invalid choice: 'bogus'" in capsys.readouterr().err


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
        expected = _invalid_subcommand_report(capsys)
        config.write_text("[default]\ncli_history = enabled\n")
        monkeypatch.setenv("AWS_CLI_HISTORY_FILE", str(config.parent))
        assert cli.main(["bogus"]) == 252
        assert capsys.readouterr().err == expected


class TestCredentialChainSettings:
    """An IMDS setting botocore refuses fails every command at rc 255.

    botocore converts and checks these settings while it builds the credential
    provider chain, which every command does when it opens its session (the
    credential cache is installed there). The report is botocore's own text.
    """

    _SETTINGS: ClassVar[list[tuple[str, str, str, str]]] = [
        (
            "AWS_METADATA_SERVICE_TIMEOUT",
            "metadata_service_timeout",
            "abc",
            "invalid literal for int() with base 10: 'abc'",
        ),
        (
            "AWS_METADATA_SERVICE_NUM_ATTEMPTS",
            "metadata_service_num_attempts",
            "1.5",
            "invalid literal for int() with base 10: '1.5'",
        ),
        (
            "AWS_EC2_METADATA_SERVICE_ENDPOINT_MODE",
            "ec2_metadata_service_endpoint_mode",
            "bogus",
            "Invalid EC2 Instance Metadata endpoint mode: bogus "
            "Valid endpoint modes (case-insensitive): ('ipv4', 'ipv6').",
        ),
        (
            "AWS_EC2_METADATA_SERVICE_ENDPOINT",
            "ec2_metadata_service_endpoint",
            "not_a_url",
            "Invalid endpoint EC2 Instance Metadata endpoint: not_a_url",
        ),
    ]

    @pytest.fixture(autouse=True)
    def _clean(self, config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config.write_text("[default]\nregion = us-east-1\n")
        for env_var, _key, _value, _report in self._SETTINGS:
            monkeypatch.delenv(env_var, raising=False)

    @pytest.mark.parametrize("argv", _SESSION_COMMANDS, ids=_SESSION_COMMAND_IDS)
    @pytest.mark.parametrize(("env_var", "key", "value", "report"), _SETTINGS)
    def test_a_bad_environment_value_fails_every_command(
        self,
        env_var: str,
        key: str,
        value: str,
        report: str,
        argv: list[str],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv(env_var, value)
        assert cli.main(argv) == 255
        captured = capsys.readouterr()
        assert captured.err == f"boto3-s3: [ERROR]: {report}\n"
        assert captured.out == ""

    @pytest.mark.parametrize(("env_var", "key", "value", "report"), _SETTINGS)
    def test_the_config_key_is_read_like_the_variable(
        self,
        env_var: str,
        key: str,
        value: str,
        report: str,
        config: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        config.write_text(f"[default]\nregion = us-east-1\n{key} = {value}\n")
        assert cli.main(_PRESIGN) == 255
        assert capsys.readouterr().err == f"boto3-s3: [ERROR]: {report}\n"

    def test_the_selected_profile_s_key_is_the_one_read(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config.write_text(
            "[default]\nregion = us-east-1\n[profile p]\nmetadata_service_timeout = abc\n"
        )
        assert cli.main(_PRESIGN) == 0
        capsys.readouterr()
        assert cli.main(["--profile", "p", *_PRESIGN]) == 255
