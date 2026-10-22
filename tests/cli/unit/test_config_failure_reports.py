"""What the config files do to a run (design/cli.md section 6).

botocore reads aws's config and credentials files when a command opens its
session, and that read is where a broken setting stops the run:

- a file that is not valid INI fails the command with botocore's
  `Unable to parse config file` at rc 255;
- an unknown `cli_timestamp_format` in the selected profile fails it at
  rc 253, an unknown `cli_binary_format` at rc 255;
- an IMDS setting botocore refuses while it builds the credential chain fails
  it at rc 255.

`help` and `--version` open no session, so none of these reach them. The rest
of the file pins that an unknown `AWS_CLI_OUTPUT_ENCODING` codec stops the
run, and that `--cli-error-format` and `cli_history` change nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from boto3_s3_cli import cli

# Commands that open a session: two that succeed without a server (so a
# setting that is accepted shows as rc 0) and a listing.
_SESSION_COMMANDS = [
    ["presign", "s3://bucket/key"],
    ["rm", "s3://bucket/key", "--dryrun"],
    ["ls", "s3://bucket/p/"],
]
_SESSION_COMMAND_IDS = ["presign", "rm-dryrun", "ls"]
# The one used where any session-opening command will do: every check below
# runs where the session opens, which _SESSION_COMMANDS shows once.
_PRESIGN = ["presign", "s3://bucket/key"]


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
    """An invalid INI file fails every command at rc 255, naming the file."""

    @pytest.mark.parametrize("argv", _SESSION_COMMANDS, ids=_SESSION_COMMAND_IDS)
    def test_it_fails_every_command(
        self, config: Path, argv: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        config.write_text("[[[broken\n")
        assert cli.main(argv) == 255
        assert str(config) in capsys.readouterr().err

    def test_a_broken_credentials_file_reports_its_own_path(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        credentials = config.parent / "credentials"
        credentials.write_text("[[[broken\n")
        assert cli.main(_PRESIGN) == 255
        assert str(credentials) in capsys.readouterr().err

    @pytest.mark.parametrize("argv", [["help"], ["--version"]], ids=["help", "version"])
    def test_the_informational_exits_do_not_read_it(
        self, config: Path, argv: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        # They open no session, so a broken config file never reaches them
        # (aws reads the config first and fails them too; the difference is
        # accepted, design/cli.md section 6).
        config.write_text("[[[broken\n")
        assert cli.main(argv) == 0
        assert capsys.readouterr().err == ""


class TestInvalidTimestampFormat:
    """An unknown `cli_timestamp_format` fails every command at rc 253."""

    def test_it_fails_the_run(self, config: Path, capsys: pytest.CaptureFixture[str]) -> None:
        config.write_text("[default]\ncli_timestamp_format = bogus\n")
        assert cli.main(_PRESIGN) == 253
        # aws's own report, through its Configuration handler's envelope.
        assert capsys.readouterr().err == (
            "boto3-s3: [ERROR]: An error occurred (Configuration): Unknown "
            'cli_timestamp_format value: bogus, valid values are "wire" or "iso8601"\n'
        )

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
    def test_an_empty_or_wrong_case_value_is_refused(self, config: Path, value: str) -> None:
        # An empty value is a value, not an absent key, and the comparison is
        # case-sensitive (both measured on aws).
        config.write_text(f"[default]\ncli_timestamp_format = {value}\n")
        assert cli.main(_PRESIGN) == 253

    def test_the_key_is_case_insensitive(self, config: Path) -> None:
        # configparser lowercases option names, as botocore's parse does.
        config.write_text("[default]\nCLI_TIMESTAMP_FORMAT = bogus\n")
        assert cli.main(_PRESIGN) == 253


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

    def test_the_flag_selects_it(self, config: Path) -> None:
        config.write_text("[default]\n[profile p]\ncli_timestamp_format = bogus\n")
        assert cli.main(["--profile", "p", *_PRESIGN]) == 253

    @pytest.mark.parametrize("env", ["AWS_PROFILE", "AWS_DEFAULT_PROFILE"])
    def test_the_env_vars_select_it(
        self, config: Path, env: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config.write_text("[default]\n[profile p]\ncli_binary_format = bogus\n")
        monkeypatch.setenv(env, "p")
        assert cli.main(_PRESIGN) == 255

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

    def test_an_empty_flag_leaves_the_env_chain_in_force(self, config: Path) -> None:
        # aws's truthy guard: `--profile ""` binds nothing, so the default
        # section still decides.
        config.write_text("[default]\ncli_timestamp_format = bogus\n")
        assert cli.main(["--profile", "", *_PRESIGN]) == 253

    def test_the_credentials_file_is_part_of_the_scoped_config(self, config: Path) -> None:
        # botocore merges the credentials file into the profile map, so a
        # setting written there is read like any other.
        config.write_text("[default]\n")
        (config.parent / "credentials").write_text("[default]\ncli_timestamp_format = bogus\n")
        assert cli.main(_PRESIGN) == 253


class TestInvalidBinaryFormat:
    """An unknown `cli_binary_format` fails every command at rc 255."""

    def test_it_fails_the_run(self, config: Path, capsys: pytest.CaptureFixture[str]) -> None:
        config.write_text("[default]\ncli_binary_format = bogus\n")
        assert cli.main(_PRESIGN) == 255
        assert "cli_binary_format" in capsys.readouterr().err

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
    def test_an_empty_or_wrong_case_value_is_refused(self, config: Path, value: str) -> None:
        # An empty value is a value, and aws's lookup is case-sensitive (both
        # measured on aws: rc 255).
        config.write_text(f"[default]\ncli_binary_format = {value}\n")
        assert cli.main(_PRESIGN) == 255


class TestUnknownOutputEncoding:
    """An unknown `AWS_CLI_OUTPUT_ENCODING` codec ends the run at rc 255.

    aws refuses such a codec before any command runs; nothing else here reads
    the variable.
    """

    @pytest.mark.parametrize("argv", [["ls", "s3://bucket/p/"], ["help"]], ids=["listing", "help"])
    def test_it_stops_the_run(
        self, argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("AWS_CLI_OUTPUT_ENCODING", "nosuch")
        assert cli.main(argv) == 255
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "AWS_CLI_OUTPUT_ENCODING" in captured.err

    def test_an_empty_value_is_an_unknown_codec(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # aws tests `in os.environ`, not truthiness: the empty string reaches
        # the codec lookup and fails it (measured).
        monkeypatch.setenv("AWS_CLI_OUTPUT_ENCODING", "")
        assert cli.main(["help"]) == 255

    def test_a_valid_codec_leaves_a_clean_run_alone(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The gate rejects nothing a codec lookup accepts.
        monkeypatch.setenv("AWS_CLI_OUTPUT_ENCODING", "utf-8")
        assert cli.main(["help"]) == 0
        assert capsys.readouterr().err == ""

    def test_the_version_flag_still_wins(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("AWS_CLI_OUTPUT_ENCODING", "nosuch")
        assert cli.main(["--version"]) == 0
        assert capsys.readouterr().err == ""


_ERROR_FORMATS = ["enhanced", "legacy", "json", "yaml", "text", "table"]


class TestTheErrorFormatIsAcceptedAndIgnored:
    """`--cli-error-format` takes aws's values, which change nothing here.

    aws re-renders its error reports by the value; this command keeps its
    one-line reports whatever it is (docs/cli/aws-differences.md section 3).
    """

    @pytest.mark.parametrize("value", _ERROR_FORMATS)
    def test_every_value_is_accepted(
        self, config: Path, value: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli.main(["--cli-error-format", value, "help"]) == 0
        assert capsys.readouterr().err == ""

    def test_an_unknown_value_is_still_rejected(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Ignoring the value does not mean skipping its choice list.
        assert cli.main(["--cli-error-format", "bogus", "ls"]) == 252
        assert "--cli-error-format" in capsys.readouterr().err


class TestCliHistoryIsNotRead:
    """`cli_history` reaches nothing here (docs/cli/aws-differences.md section 2).

    aws records the run in a history database when the selected profile
    enables it. This command has no history mechanism, so every value is
    inert.
    """

    @pytest.mark.parametrize("value", ["enabled", "disabled", "bogus"])
    def test_no_value_changes_the_run(
        self, config: Path, value: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config.write_text(f"[default]\ncli_history = {value}\n")
        assert cli.main(["help"]) == 0
        assert capsys.readouterr().err == ""


class TestCredentialChainSettings:
    """An IMDS setting botocore refuses fails every command at rc 255.

    botocore converts and checks these settings while it builds the credential
    provider chain, which every command does when it opens its session (the
    credential cache is installed there). `AWS_METADATA_SERVICE_TIMEOUT`
    stands for the four such settings (the timeout, the attempt count, the
    endpoint and its mode): botocore checks each of them in the same build.
    """

    @pytest.fixture(autouse=True)
    def _clean(self, config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config.write_text("[default]\nregion = us-east-1\n")
        monkeypatch.delenv("AWS_METADATA_SERVICE_TIMEOUT", raising=False)

    def test_a_bad_environment_value_fails_the_run(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("AWS_METADATA_SERVICE_TIMEOUT", "abc")
        assert cli.main(_PRESIGN) == 255
        captured = capsys.readouterr()
        assert "abc" in captured.err
        assert captured.out == ""

    def test_the_config_key_is_read_like_the_variable(self, config: Path) -> None:
        config.write_text("[default]\nregion = us-east-1\nmetadata_service_timeout = abc\n")
        assert cli.main(_PRESIGN) == 255

    def test_the_selected_profile_s_key_is_the_one_read(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config.write_text(
            "[default]\nregion = us-east-1\n[profile p]\nmetadata_service_timeout = abc\n"
        )
        assert cli.main(_PRESIGN) == 0
        capsys.readouterr()
        assert cli.main(["--profile", "p", *_PRESIGN]) == 255
