"""The ``~/.aws/cli/alias`` file's ``[command s3]`` section (design/cli.md section 9).

aws injects that section into the ``aws s3`` subcommand table, which is this
CLI's whole surface, so the same file works here. Every expectation below was
measured against the pinned aws-cli under the usual mapping (``aws [options]
s3 <subcommand>`` -> ``boto3-s3 [options] <subcommand>``, and the blank line
aws writes before each report - design/testing.md section 9).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from boto3_s3_cli import cli
from boto3_s3_cli.commands.base import Context
from tests.utils.fakes3 import MTIME
from tests.utils.harness import run_cli_in_process, run_recorded, unused_ctx

_USAGE_BLOCK = (
    "usage: boto3-s3 [options] <subcommand> [parameters]\n"
    "To see help text, you can run:\n"
    "\n"
    "  boto3-s3 help\n"
    "  boto3-s3 <subcommand> help\n"
)

# One empty listing page, enough for any `ls` the aliases below expand to.
_EMPTY_PAGE: list[dict[str, Any] | Exception] = [{"Contents": [], "CommonPrefixes": []}]

# A listing that is one object, so `ls`'s own output can be seen.
_ONE_OBJECT: list[dict[str, Any] | Exception] = [
    {"Contents": [{"Key": "k", "Size": 3, "LastModified": MTIME}], "CommonPrefixes": []}
]


@pytest.fixture
def alias_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``~/.aws/cli/alias`` for this test, under a private HOME, absent until written."""
    home = tmp_path / "home"
    (home / ".aws" / "cli").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows expanduser reads this
    return home / ".aws" / "cli" / "alias"


class TestInternalAliases:
    """A value that is not ``!``-led expands to arguments and is parsed again."""

    def test_the_expansion_precedes_the_typed_arguments(self, alias_file: Path) -> None:
        # `lsr = ls --recursive` + `lsr s3://bkt` is `ls --recursive s3://bkt`:
        # aws issues one ListObjectsV2 with no delimiter (measured URL:
        # `?list-type=2&prefix=&encoding-type=url`).
        alias_file.write_text("[command s3]\nlsr = ls --recursive\n")
        result, calls = run_recorded(_EMPTY_PAGE, ["lsr", "s3://bkt"])
        assert result.rc == 0
        assert [call.operation for call in calls] == ["ListObjectsV2"]
        assert calls[0].params["Bucket"] == "bkt"
        assert "Delimiter" not in calls[0].params

    def test_the_typed_arguments_are_appended(self, alias_file: Path) -> None:
        # aws's URL for the same run carries the typed `--page-size 7` as
        # `max-keys=7`, so the option reaches the leaf after the expansion.
        alias_file.write_text("[command s3]\nlsr = ls --recursive\n")
        _result, calls = run_recorded(_EMPTY_PAGE, ["lsr", "s3://bkt", "--page-size", "7"])
        assert calls[0].params["MaxKeys"] == 7

    def test_the_value_is_split_with_quoting(self, alias_file: Path) -> None:
        # `ls "s3://sp ace"` is one argument, so the space stays inside the
        # bucket name instead of starting a second path (aws reaches the same
        # single name: its parameter validation rejects `sp ace`, rc 252).
        alias_file.write_text('[command s3]\nq = ls "s3://sp ace"\n')
        _result, calls = run_recorded(_EMPTY_PAGE, ["q"])
        assert calls[0].params["Bucket"] == "sp ace"

    def test_a_multiline_value_is_one_argument_list(self, alias_file: Path) -> None:
        alias_file.write_text("[command s3]\nm = ls\n  --recursive\n")
        _result, calls = run_recorded(_EMPTY_PAGE, ["m", "s3://bkt"])
        assert "Delimiter" not in calls[0].params

    def test_an_unbalanced_quote_is_reported_at_255(
        self, alias_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        alias_file.write_text("[command s3]\nq = ls 's3://x\n")
        assert cli.main(["q"], ctx=unused_ctx()) == 255
        assert capsys.readouterr().err == (
            'boto3-s3: [ERROR]: Value of alias "q" could not be parsed. '
            "Received error: No closing quotation when parsing:\nls 's3://x\n"
        )

    def test_an_empty_value_leaves_the_arguments_alone(
        self, alias_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Nothing to expand, so what the user typed is resolved on its own:
        # with no arguments that is the missing-subcommand report (measured).
        alias_file.write_text("[command s3]\ne =\n")
        assert cli.main(["e"], ctx=unused_ctx()) == 252
        assert capsys.readouterr().err == (
            "boto3-s3: [ERROR]: An error occurred (ParamValidation): "
            "usage: boto3-s3 [options] <subcommand> [parameters]\n"
            "boto3-s3: [ERROR]: too few arguments\n"
        )

    def test_an_expansion_that_names_nothing_is_an_invalid_choice(
        self, alias_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        alias_file.write_text("[command s3]\nx = nosuch --foo\n")
        assert cli.main(["x"], ctx=unused_ctx()) == 252
        assert capsys.readouterr().err == (
            "boto3-s3: [ERROR]: An error occurred (ParamValidation): "
            f"argument subcommand: Found invalid choice 'nosuch'\n\n\n{_USAGE_BLOCK}"
        )

    def test_an_alias_may_expand_to_another_alias(self, alias_file: Path) -> None:
        alias_file.write_text("[command s3]\na = b\nb = ls --recursive\n")
        _result, calls = run_recorded(_EMPTY_PAGE, ["a", "s3://bkt"])
        assert "Delimiter" not in calls[0].params

    def test_an_alias_that_expands_to_itself_is_reported_at_255(
        self, alias_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        alias_file.write_text("[command s3]\na = a\n")
        assert cli.main(["a"], ctx=unused_ctx()) == 255
        assert capsys.readouterr().err == ("boto3-s3: [ERROR]: maximum recursion depth exceeded\n")

    def test_the_name_is_lowercased_by_the_file_s_parser(self, alias_file: Path) -> None:
        # configparser lowers option names, so an alias written `LSR` is the
        # subcommand `lsr` - and `LSR` itself is no subcommand at all.
        alias_file.write_text("[command s3]\nLSR = ls --recursive\n")
        result, _calls = run_recorded(_EMPTY_PAGE, ["lsr", "s3://bkt"])
        assert result.rc == 0
        assert run_cli_in_process(["LSR", "s3://bkt"], ctx=unused_ctx()).rc == 252

    def test_a_stray_space_still_names_the_section(self, alias_file: Path) -> None:
        alias_file.write_text("[command  s3]\nlsr = ls --recursive\n")
        result, _calls = run_recorded(_EMPTY_PAGE, ["lsr", "s3://bkt"])
        assert result.rc == 0

    def test_the_help_token_still_pages_after_an_expansion(self, alias_file: Path) -> None:
        # `l = ls` + `l help` reaches the leaf as exactly ['help'], which is
        # the leaf's help rule; aws pages the `ls` page there (rc 0).
        alias_file.write_text("[command s3]\nl = ls\n")
        result = run_cli_in_process(["l", "help"], ctx=unused_ctx())
        assert result.rc == 0
        assert result.stdout.startswith("usage: boto3-s3 ls")

    def test_an_expansion_can_make_help_an_argument(self, alias_file: Path) -> None:
        # With something ahead of it the token is no longer the whole
        # remainder, so it is a path: aws lists the bucket named `help`
        # (measured URL `/help?list-type=2...`).
        alias_file.write_text("[command s3]\nlsr = ls --recursive\n")
        _result, calls = run_recorded(_EMPTY_PAGE, ["lsr", "help"])
        assert calls[0].params["Bucket"] == "help"


class TestAliasGlobals:
    """A global option in the value is taken out and applied to the run."""

    def _region_ctx(self, seen: list[str | None]) -> Context:
        from tests.utils.recorder import make_recording_client

        def factory(args: Any) -> Any:
            seen.append(args.region)
            client, _calls = make_recording_client(_EMPTY_PAGE)
            return client

        return Context(client_factory=factory)  # pyright: ignore[reportArgumentType]

    def test_a_global_in_the_value_is_applied(self, alias_file: Path) -> None:
        alias_file.write_text("[command s3]\ng = ls --region eu-west-1\n")
        seen: list[str | None] = []
        assert cli.main(["g", "s3://bkt"], ctx=self._region_ctx(seen)) == 0
        assert seen == ["eu-west-1"]

    def test_the_value_s_global_beats_the_typed_one(self, alias_file: Path) -> None:
        # Measured on the pinned aws-cli: the request signs with the alias's
        # region whichever side of the alias name the typed one sits on.
        alias_file.write_text("[command s3]\ng = ls --region eu-west-1\n")
        seen: list[str | None] = []
        assert (
            cli.main(["g", "s3://bkt", "--region", "ap-south-1"], ctx=self._region_ctx(seen)) == 0
        )
        assert (
            cli.main(["--region", "ap-south-1", "g", "s3://bkt"], ctx=self._region_ctx(seen)) == 0
        )
        assert seen == ["eu-west-1", "eu-west-1"]

    @pytest.mark.parametrize("option", ["--debug", "--profile p"])
    def test_debug_and_profile_are_refused(
        self, alias_file: Path, option: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        alias_file.write_text(f"[command s3]\nx = ls {option}\n")
        assert cli.main(["x", "s3://bkt"], ctx=unused_ctx()) == 255
        name = option.split()[0]
        assert capsys.readouterr().err == (
            f'boto3-s3: [ERROR]: Global parameter "{name}" detected in alias "x" '
            "which is not supported in subcommand aliases.\n"
        )

    def test_debug_is_blamed_before_the_other_failures(
        self, alias_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws scans its own namespace order (debug ahead of profile) and does
        # so before it resolves the values it keeps, so both a second refused
        # global and a bad `--endpoint-url` lose to it (measured).
        alias_file.write_text("[command s3]\nx = ls --debug --profile p --endpoint-url bad\n")
        assert cli.main(["x", "s3://bkt"], ctx=unused_ctx()) == 255
        assert '"--debug"' in capsys.readouterr().err

    def test_a_bad_global_in_the_value_is_resolved_like_a_typed_one(
        self, alias_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        alias_file.write_text("[command s3]\nu = ls --endpoint-url badurl\n")
        assert cli.main(["u", "s3://bkt"], ctx=unused_ctx()) == 252
        assert capsys.readouterr().err == (
            "boto3-s3: [ERROR]: An error occurred (ParamValidation): Bad value for "
            '--endpoint-url "badurl": scheme is missing.  '
            "Must be of the form http://<hostname>/ or https://<hostname>/\n"
        )

    def test_a_global_that_fails_to_parse_is_the_usage_error(
        self, alias_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        alias_file.write_text("[command s3]\nb = ls --output bogus\n")
        assert cli.main(["b", "s3://bkt"], ctx=unused_ctx()) == 252
        assert capsys.readouterr().err == (
            "boto3-s3: [ERROR]: An error occurred (ParamValidation): "
            f"argument --output: Found invalid choice 'bogus'\n\n\n{_USAGE_BLOCK}"
        )

    def test_version_in_the_value_prints_and_exits(
        self, alias_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        alias_file.write_text("[command s3]\nv = ls --version\n")
        assert cli.main(["v"], ctx=unused_ctx()) == 0
        assert capsys.readouterr().out.startswith("boto3-s3-cli/")


class TestShadowingABuiltIn:
    """An alias named after a subcommand proxies to it, minus one token."""

    def test_the_expansion_becomes_that_subcommand_s_options(self, alias_file: Path) -> None:
        alias_file.write_text("[command s3]\nls = ls --recursive\n")
        _result, calls = run_recorded(_EMPTY_PAGE, ["ls", "s3://bkt"])
        assert "Delimiter" not in calls[0].params

    def test_the_first_token_is_dropped_whatever_it_names(self, alias_file: Path) -> None:
        # aws drops it without looking, so `ls = cp` still runs `ls` - and
        # non-recursively, since nothing else was in the value (measured).
        alias_file.write_text("[command s3]\nls = cp\n")
        _result, calls = run_recorded(_ONE_OBJECT, ["ls", "s3://bkt"])
        assert [call.operation for call in calls] == ["ListObjectsV2"]
        assert calls[0].params["Delimiter"] == "/"

    def test_an_empty_value_eats_the_first_typed_argument(self, alias_file: Path) -> None:
        # The drop happens even when the expansion is empty, so `ls s3://bkt`
        # becomes a bare `ls`: aws lists the account's buckets (measured URL
        # `http://127.0.0.1:9/`).
        alias_file.write_text("[command s3]\nls =\n")
        _result, calls = run_recorded([{"Buckets": [], "Owner": {}}], ["ls", "s3://bkt"])
        assert [call.operation for call in calls] == ["ListBuckets"]


@pytest.mark.skipif(sys.platform == "win32", reason="the external aliases here are sh commands")
class TestExternalAliases:
    """A ``!``-led value is a shell command line whose status is the run's."""

    def test_the_exit_status_is_the_run_s(self, alias_file: Path) -> None:
        alias_file.write_text("[command s3]\nrc = !exit 7\n")
        assert cli.main(["rc"], ctx=unused_ctx()) == 7

    def test_an_unknown_program_is_the_shell_s_127(self, alias_file: Path) -> None:
        alias_file.write_text("[command s3]\nnope = !no-such-program-xyz 2>/dev/null\n")
        assert cli.main(["nope"], ctx=unused_ctx()) == 127

    def test_the_arguments_are_appended_shell_quoted(
        self, alias_file: Path, tmp_path: Path
    ) -> None:
        # Each argument arrives whole, so a space inside one does not split it
        # (aws quotes them; measured: `hi 'a b' --x` echoes `a b --x`).
        written = tmp_path / "args"
        alias_file.write_text(f'[command s3]\nhi = !sh -c \'printf "%s\\n" "$@" > {written}\' sh\n')
        assert cli.main(["hi", "a b", "--x"], ctx=unused_ctx()) == 0
        assert written.read_text() == "a b\n--x\n"

    def test_a_global_the_user_typed_is_not_appended(
        self, alias_file: Path, tmp_path: Path
    ) -> None:
        # The globals pass consumes it before the alias is reached, on both
        # tools (measured: `hi --region eu-west-1` echoes nothing extra), so
        # the shell command runs with no arguments at all.
        written = tmp_path / "args"
        alias_file.write_text(f"[command s3]\nhi = !sh -c 'echo \"$#\" > {written}' sh\n")
        assert cli.main(["hi", "--region", "eu-west-1"], ctx=unused_ctx()) == 0
        assert written.read_text() == "0\n"

    def test_a_command_line_the_shell_cannot_be_given_is_reported_at_255(
        self, alias_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A NUL cannot go into a command line, and the launch raises before
        # any shell exists. aws registers no handler of its own for that, so
        # the failure reaches its entry point's general one: `str(exc)` at rc
        # 255 (measured), never a traceback.
        alias_file.write_text("[command s3]\ne = !echo A\x00B\n")
        assert cli.main(["e"], ctx=unused_ctx()) == 255
        assert capsys.readouterr().err == "boto3-s3: [ERROR]: embedded null byte\n"

    def test_an_os_refusal_to_launch_is_that_same_report(
        self,
        alias_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # The other measured shape - a command line past the OS argument limit
        # - without building the multi-megabyte value it takes to provoke it.
        import subprocess

        def refuse(*_args: object, **_kwargs: object) -> int:
            raise OSError(7, "Argument list too long", "/bin/sh")

        monkeypatch.setattr(subprocess, "call", refuse)
        alias_file.write_text("[command s3]\ne = !echo hi\n")
        assert cli.main(["e"], ctx=unused_ctx()) == 255
        assert capsys.readouterr().err == (
            "boto3-s3: [ERROR]: [Errno 7] Argument list too long: '/bin/sh'\n"
        )


class TestReportsAndSections:
    """What the alias table does to the reports, and which sections count."""

    def test_an_alias_can_be_the_near_miss_of_a_typo(
        self, alias_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws checks the name against the table its injector added to, so the
        # suggestion pool includes aliases (measured: `lsxx` -> `* lsx`).
        alias_file.write_text("[command s3]\nlsx = ls --recursive\n")
        assert cli.main(["lsxx"], ctx=unused_ctx()) == 252
        assert capsys.readouterr().err == (
            "boto3-s3: [ERROR]: An error occurred (ParamValidation): "
            "argument subcommand: Found invalid choice 'lsxx'\n"
            "\nMaybe you meant:\n"
            f"\n  * lsx\n\n{_USAGE_BLOCK}"
        )

    @pytest.mark.parametrize(
        "text",
        [
            "[toplevel]\nwho = !echo TOPLEVEL\n",
            "[command ec2]\nwho = !echo EC2\n",
            "[Command s3]\nwho = !echo CASED\n",
        ],
    )
    def test_other_sections_declare_nothing_here(self, alias_file: Path, text: str) -> None:
        # `[toplevel]` names services (no counterpart here), another command's
        # section is another command's, and the section name is case-sensitive
        # - all measured as an invalid choice on aws.
        alias_file.write_text(text)
        assert cli.main(["who"], ctx=unused_ctx()) == 252

    @pytest.mark.parametrize(
        "header",
        [
            "command\ts3",
            " command s3",
            "\tcommand s3",
            "command\vs3",
            "command\fs3",
            "command\xa0s3",
            "command　s3",
            "commands3",
        ],
        ids=[
            "tab",
            "leading-space",
            "leading-tab",
            "vtab",
            "formfeed",
            "nbsp",
            "ideographic",
            "none",
        ],
    )
    def test_only_a_command_space_prefix_names_the_section(
        self, alias_file: Path, header: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws re-keys a section onto a command lineage only when the raw name
        # starts with `command ` - that one ASCII space - and nothing here
        # does, so none of them declares an alias at all. Measured: every one
        # is the invalid-choice report on the pinned aws-cli.
        alias_file.write_text(f"[{header}]\nsay = ls s3://bkt\n")
        assert cli.main(["say"], ctx=unused_ctx()) == 252
        assert "Found invalid choice 'say'" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "header",
        ["command s3", "command  s3", "command \ts3", "command s3 ", "command s3\t"],
        ids=["plain", "two-spaces", "space-tab", "trailing-space", "trailing-tab"],
    )
    def test_whitespace_past_that_prefix_still_names_it(
        self, alias_file: Path, header: str
    ) -> None:
        # Once the prefix is there aws splits the whole name on whitespace, so
        # every spelling below is the one section (measured: the alias runs).
        alias_file.write_text(f"[{header}]\nlsr = ls --recursive\n")
        result, _calls = run_recorded(_EMPTY_PAGE, ["lsr", "s3://bkt"])
        assert result.rc == 0

    def test_a_leaf_section_follows_the_same_naming_rule(self, alias_file: Path) -> None:
        # The prefix is what decides, not the separators after it: a tab
        # between `s3` and `ls` still names the leaf and breaks it, while a
        # tab in the prefix leaves the subcommand untouched (measured).
        alias_file.write_text("[command s3\tls]\nr = --recursive\n")
        assert cli.main(["ls", "s3://bkt"], ctx=unused_ctx()) == 255
        alias_file.write_text("[command\ts3 ls]\nr = --recursive\n")
        result, _calls = run_recorded(_EMPTY_PAGE, ["ls", "s3://bkt"])
        assert result.rc == 0

    def test_a_leaf_section_breaks_that_subcommand(
        self, alias_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws's injection into a leaf table breaks the parser it builds there,
        # so every `ls` invocation reports that failure at rc 255 (measured;
        # the entries in the section are never reached).
        alias_file.write_text("[command s3 ls]\nr = --recursive\n")
        assert cli.main(["ls", "s3://bkt"], ctx=unused_ctx()) == 255
        assert capsys.readouterr().err == (
            "boto3-s3: [ERROR]: 'required' is an invalid argument for positionals\n"
        )

    def test_a_leaf_section_breaks_that_subcommand_s_help_too(self, alias_file: Path) -> None:
        alias_file.write_text("[command s3 ls]\nr = --recursive\n")
        assert cli.main(["ls", "help"], ctx=unused_ctx()) == 255

    def test_a_leaf_section_leaves_the_other_subcommands_alone(self, alias_file: Path) -> None:
        alias_file.write_text("[command s3 ls]\nr = --recursive\n")
        result, calls = run_recorded([{"Location": "/bkt"}], ["mb", "s3://bkt"])
        assert result.rc == 0
        assert [call.operation for call in calls] == ["CreateBucket"]

    def test_an_empty_leaf_section_is_inert(self, alias_file: Path) -> None:
        alias_file.write_text("[command s3 ls]\n")
        result, _calls = run_recorded(_EMPTY_PAGE, ["ls", "s3://bkt"])
        assert result.rc == 0


class TestWhichLeafSectionApplies:
    """A subcommand's leaf section is named after the lineage it was reached by.

    aws gives a subcommand its lineage while it builds the table the subcommand
    sits in, so one reached through this CLI's table looks for
    ``[command s3 <name>]``; an alias that repeats a built-in's name replaces it
    in that table and keeps it aside as a proxy, and that copy - never given a
    lineage - looks for ``[command <name>]`` instead. Every expectation below
    was measured on the pinned aws-cli in both directions.
    """

    _CRASH = "boto3-s3: [ERROR]: 'required' is an invalid argument for positionals\n"
    _SHADOW = "[command s3]\nls = ls --recursive\n"

    def test_a_shadowed_built_in_breaks_on_the_bare_section(
        self, alias_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        alias_file.write_text(f"[command ls]\nfoo = ls\n{self._SHADOW}")
        assert cli.main(["ls", "s3://bkt"], ctx=unused_ctx()) == 255
        assert capsys.readouterr().err == self._CRASH

    def test_a_shadowed_built_in_ignores_the_command_table_section(self, alias_file: Path) -> None:
        # `[command s3 ls]` belongs to the lineage the alias took `ls` out of,
        # so the run goes through - recursively, as the alias asked.
        alias_file.write_text(f"[command s3 ls]\nfoo = ls\n{self._SHADOW}")
        result, calls = run_recorded(_EMPTY_PAGE, ["ls", "s3://bkt"])
        assert result.rc == 0
        assert "Delimiter" not in calls[0].params

    def test_a_bare_section_leaves_a_directly_invoked_built_in_alone(
        self, alias_file: Path
    ) -> None:
        alias_file.write_text("[command ls]\nfoo = ls\n")
        result, _calls = run_recorded(_EMPTY_PAGE, ["ls", "s3://bkt"])
        assert result.rc == 0

    def test_the_shadow_proxy_is_named_by_the_alias_not_by_its_value(
        self, alias_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `ls = rm --dryrun` still proxies to `ls`, so `[command ls]` is what
        # breaks the run even though the value names `rm`.
        alias_file.write_text("[command ls]\nfoo = ls\n[command s3]\nls = rm --dryrun\n")
        assert cli.main(["ls", "s3://bkt"], ctx=unused_ctx()) == 255
        assert capsys.readouterr().err == self._CRASH

    @pytest.mark.parametrize("section", ["command rm", "command s3 rm"])
    def test_the_section_of_the_command_the_value_names_does_nothing(
        self, alias_file: Path, section: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The other half of the same rule: neither `rm` section is consulted,
        # so `ls` runs and rejects the option the value carried (measured).
        alias_file.write_text(f"[{section}]\nfoo = ls\n[command s3]\nls = rm --dryrun\n")
        assert cli.main(["ls", "s3://bkt"], ctx=unused_ctx()) == 252
        assert capsys.readouterr().err == (
            "boto3-s3: [ERROR]: An error occurred (ParamValidation): Unknown options: --dryrun\n"
        )

    def test_a_chain_that_ends_in_a_shadow_uses_the_bare_section(
        self, alias_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        alias_file.write_text("[command ls]\nfoo = ls\n[command s3]\na = ls\nls = ls --recursive\n")
        assert cli.main(["a", "s3://bkt"], ctx=unused_ctx()) == 255
        assert capsys.readouterr().err == self._CRASH

    def test_an_alias_that_shadows_nothing_keeps_the_command_table_section(
        self, alias_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `xls = ls` re-enters the resolution instead of proxying, so the
        # built-in is reached through the table and carries its lineage again.
        alias_file.write_text("[command s3 ls]\nfoo = ls\n[command s3]\nxls = ls\n")
        assert cli.main(["xls", "s3://bkt"], ctx=unused_ctx()) == 255
        assert capsys.readouterr().err == self._CRASH
        alias_file.write_text("[command ls]\nfoo = ls\n[command s3]\nxls = ls\n")
        result, _calls = run_recorded(_EMPTY_PAGE, ["xls", "s3://bkt"])
        assert result.rc == 0

    def test_the_help_token_does_not_escape_the_shadow_section(
        self, alias_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The table is built before the parse, so the crash beats the help
        # page here exactly as it does on the direct path.
        alias_file.write_text(f"[command ls]\nfoo = ls\n{self._SHADOW}")
        assert cli.main(["ls", "help"], ctx=unused_ctx()) == 255
        assert capsys.readouterr().err == self._CRASH

    @pytest.mark.skipif(sys.platform == "win32", reason="the external alias here is an sh command")
    def test_an_external_shadow_alias_consults_no_leaf_section(self, alias_file: Path) -> None:
        # An external alias replaces the built-in outright rather than proxying
        # to it, so no leaf table is ever built and neither section applies.
        alias_file.write_text(
            "[command ls]\nfoo = ls\n[command s3 ls]\nfoo = ls\n[command s3]\nls = !exit 7\n"
        )
        assert cli.main(["ls", "s3://bkt"], ctx=unused_ctx()) == 7


class TestUnreadableAliasFile:
    """A file that exists but cannot be read aborts the run before everything."""

    _UNPARSEABLE = "boto3-s3: [ERROR]: Unable to parse config file: {}\n"

    def test_a_file_that_is_not_ini_replaces_the_run(
        self, alias_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        alias_file.write_text("this is not ini\n")
        assert cli.main(["ls", "s3://bkt"], ctx=unused_ctx()) == 255
        assert capsys.readouterr().err == self._UNPARSEABLE.format(alias_file)

    @pytest.mark.parametrize("argv", [["--version"], ["help"], ["ls", "--output", "bogus"]])
    def test_it_beats_the_version_flag_the_help_token_and_a_bad_global(
        self, alias_file: Path, argv: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws reads the file while it is still assembling its parser, so
        # nothing the parse would have settled gets the chance (measured).
        alias_file.write_text("this is not ini\n")
        assert cli.main(argv, ctx=unused_ctx()) == 255
        assert capsys.readouterr().err == self._UNPARSEABLE.format(alias_file)

    def test_a_duplicate_alias_name_is_that_same_failure(
        self, alias_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        alias_file.write_text("[command s3]\nlsr = ls\nlsr = ls --recursive\n")
        assert cli.main(["lsr"], ctx=unused_ctx()) == 255
        assert capsys.readouterr().err == self._UNPARSEABLE.format(alias_file)

    def test_a_directory_is_the_not_found_report(
        self, alias_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        alias_file.mkdir()
        assert cli.main(["--version"], ctx=unused_ctx()) == 255
        assert capsys.readouterr().err == (
            f"boto3-s3: [ERROR]: The specified config file ({alias_file}) could not be found.\n"
        )

    def test_no_file_at_all_changes_nothing(self, alias_file: Path) -> None:
        assert not alias_file.exists()
        result, _calls = run_recorded(_EMPTY_PAGE, ["ls", "s3://bkt"])
        assert result.rc == 0
