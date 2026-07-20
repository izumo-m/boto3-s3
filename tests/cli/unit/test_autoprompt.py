"""Unit tests for ``--cli-auto-prompt``: the completion engine and the wiring.

The completion engine (model + parser + completers) is pure Python, so it is
exercised directly through an ``AutoCompleter`` with fake region/profile
providers - no ``prompt_toolkit``, no botocore, no network. The ``cli.main``
wiring (mutual exclusion, the missing-dependency degradation, and the
prompt -> re-dispatch loop) is exercised with a fake ``AutoPrompter``
injected through ``Context``, the same dependency-injection seam the other
subcommand tests use for the S3 client.
"""

from __future__ import annotations

import argparse
import importlib.util
import logging
from pathlib import Path
from typing import Any

import pytest

from boto3_s3_cli import cli
from boto3_s3_cli.autoprompt import completers as c
from boto3_s3_cli.autoprompt import resolve
from boto3_s3_cli.autoprompt.model import ROOT, build_model, subparser_map
from boto3_s3_cli.autoprompt.parser import CLIParser
from boto3_s3_cli.autoprompt.prompter import AutoPrompter
from boto3_s3_cli.commands.base import Context

_REGIONS = ["us-east-1", "us-west-2", "eu-west-1"]
_PROFILES = ["default", "minio", "prod"]


def _completer() -> c.AutoCompleter:
    model = build_model()
    return c.AutoCompleter(
        CLIParser(model),
        [
            c.RegionCompleter(c.fuzzy_filter, regions_provider=lambda: _REGIONS),
            c.ProfileCompleter(c.fuzzy_filter, profiles_provider=lambda: _PROFILES),
            c.ModelIndexCompleter(model, c.fuzzy_filter),
            c.FilePathCompleter(c.fuzzy_filter),
            c.ChoicesCompleter(model, c.fuzzy_filter),
        ],
    )


def _names(line: str) -> list[str]:
    """Completion candidate names for a partial line (sans the leading prog token)."""
    text = f"{ROOT} {line}"
    return [r.name for r in _completer().autocomplete(text, len(text))]


# --------------------------------------------------------------------------- #
# Completion engine                                                           #
# --------------------------------------------------------------------------- #


class TestCompletionEngine:
    def test_subcommand_names(self) -> None:
        assert sorted(_names("")) == [
            "cp",
            "ls",
            "mb",
            "mv",
            "presign",
            "rb",
            "rm",
            "sync",
            "website",
        ]

    def test_subcommand_names_fuzzy(self) -> None:
        # 'c' subsequence-matches cp and sync (the only names containing a 'c').
        assert sorted(_names("c")) == ["cp", "sync"]

    def test_option_names_include_command_and_global(self) -> None:
        names = _names("cp --")
        assert "--recursive" in names  # cp-specific
        assert "--storage-class" in names  # cp-specific
        assert "--region" in names  # global, injected
        assert "--no-guess-mime-type" in names  # explicit negated boolean

    def test_already_supplied_option_is_excluded(self) -> None:
        names = _names("cp --recursive --re")
        assert "--recursive" not in names  # already given
        assert "--request-payer" in names  # still offerable

    def test_command_level_choices_are_completed(self) -> None:
        # The intentional gap fix: aws does not complete --storage-class values
        # (its values live on .choices, not argument_model.enum); we do.
        assert _names("cp s3://a --storage-class ") == [
            "STANDARD",
            "REDUCED_REDUNDANCY",
            "STANDARD_IA",
            "ONEZONE_IA",
            "INTELLIGENT_TIERING",
            "GLACIER",
            "DEEP_ARCHIVE",
            "GLACIER_IR",
        ]

    def test_global_choices_are_completed(self) -> None:
        assert _names("cp s3://a --output ") == [
            "json",
            "text",
            "table",
            "yaml",
            "yaml-stream",
            "off",
        ]

    def test_region_value_completion(self) -> None:
        # Fuzzy ranking puts the leftmost/shortest matches (the us- regions)
        # first; eu-west-1 also subsequence-matches "us-" but ranks after.
        assert _names("cp s3://a --region us-")[:2] == ["us-east-1", "us-west-2"]

    def test_profile_value_completion(self) -> None:
        assert sorted(_names("cp s3://a --profile ")) == sorted(_PROFILES)

    def test_region_completion_caches_provider_across_keystrokes(self) -> None:
        # Value completion fires on every keystroke; the provider (a botocore
        # Session in production) must be consulted at most once per completer so
        # the prompt stays responsive - aws caches its session the same way.
        calls = 0

        def provider() -> list[str]:
            nonlocal calls
            calls += 1
            return _REGIONS

        completer = c.AutoCompleter(
            CLIParser(build_model()),
            [c.RegionCompleter(c.fuzzy_filter, regions_provider=provider)],
        )
        for fragment in ("us", "us-", "us-e", "us-ea"):
            line = f"{ROOT} cp s3://a --region {fragment}"
            completer.autocomplete(line, len(line))
        assert calls == 1  # resolved once, then served from cache

    def test_file_url_path_completion(self, tmp_path: Path) -> None:
        import os

        base = str(tmp_path)
        for name in ("alpha.txt", "beta.txt"):
            with open(os.path.join(base, name), "w"):
                pass
        line = f"{ROOT} cp file://{base}{os.sep}al"
        results = _completer().autocomplete(line, len(line))
        assert [r.display_text for r in results] == ["alpha.txt"]

    def test_free_text_value_has_no_completion(self) -> None:
        # --content-type takes free text and no choices: nothing to offer.
        assert _names("cp s3://a --content-type ") == []

    def test_fuzzy_ranking_prefers_shortest_leftmost(self) -> None:
        # 'st' matches --storage-class (st...) ahead of options where s..t spans
        # more characters; the storage-class option ranks first.
        names = _names("cp --st")
        assert names[0] == "--storage-class"


# --------------------------------------------------------------------------- #
# Reachability: usability tuning over the faithful port (docs/autoprompt.md section 2) #
# --------------------------------------------------------------------------- #


class TestCompletionReachability:
    """Completion reaches the natural cases the aws-cli parser would drop.

    The auto-prompt UI is charter-exempt, so the engine favors usability over a
    byte-faithful port: option values complete before any positional is typed,
    and options keep completing after a command's second positional path.
    """

    def test_option_value_completes_before_any_path(self) -> None:
        # `cp --storage-class <TAB>` with no source/dest yet: the port reset
        # current_param to the positional and offered nothing; we keep it on the
        # option so its value completer fires.
        assert _names("cp --storage-class ")[:2] == ["STANDARD", "REDUCED_REDUNDANCY"]
        assert _names("cp --acl pub") == ["public-read", "public-read-write"]
        assert _names("cp --sse ") == ["AES256", "aws:kms"]

    def test_global_value_completes_before_any_path(self) -> None:
        assert _names("cp --output ") == ["json", "text", "table", "yaml", "yaml-stream", "off"]
        assert _names("cp --region us-")[:2] == ["us-east-1", "us-west-2"]

    def test_options_complete_after_two_paths(self) -> None:
        # cp/mv/sync take two positional paths; completing options after both is
        # the normal flow. The port dropped the 2nd path into unparsed_items and
        # filtered options by the command name (a useless fuzzy subset).
        assert _names("cp s3://a s3://b --st")[0] == "--storage-class"
        assert _names("sync . s3://b --ex")[:2] == ["--exact-timestamps", "--exclude"]
        all_opts = _names("cp s3://a s3://b ")  # empty fragment -> the full set
        assert "--recursive" in all_opts and "--storage-class" in all_opts

    def test_supplied_option_excluded_after_two_paths(self) -> None:
        # Dedup works post-2-paths because the option now parses into
        # parsed_params (the parser keeps the command's options live).
        names = _names("cp s3://a s3://b --recursive --")
        assert "--recursive" not in names
        assert "--dryrun" in names

    def test_value_completes_after_two_paths(self) -> None:
        assert _names("mv ./a s3://b --storage-class ")[0] == "STANDARD"

    def test_options_complete_after_a_non_path_like_second_positional(self) -> None:
        # A bare local name as the second positional (no path character)
        # still offers options - aws drops it on a path-likeness heuristic; we
        # keep completing (usability first, docs/autoprompt.md section 3).
        assert _names("cp s3://a outdir --st")[0] == "--storage-class"
        assert _names("sync s3://a outdir --ex")[:2] == ["--exact-timestamps", "--exclude"]
        all_opts = _names("cp s3://a outdir --")
        assert "--recursive" in all_opts and "--storage-class" in all_opts

    def test_value_completes_after_a_non_path_like_second_positional(self) -> None:
        # An option value also completes after a bare second positional.
        assert _names("cp s3://a outdir --storage-class ")[0] == "STANDARD"

    def test_unknown_option_among_positionals_still_offers(self) -> None:
        # Never offer less than aws (docs/autoprompt.md section 1): aws treats a
        # `--`-containing unparsed token as path-like and offers options, so an
        # unknown --option sitting among the positionals must not suppress the
        # menu (regression guard for the relaxed second-positional gate,
        # docs/autoprompt.md section 3).
        assert "--recursive" in _names("cp s3://a --bogus s3://b --")

    def test_bare_command_offers_options(self) -> None:
        # `cp <TAB>` (no path yet) offers the option set, matching `cp src <TAB>`
        # after the first path - the port claimed the empty boundary as the
        # positional and offered nothing.
        bare = _names("cp ")
        after_path = _names("cp s3://a ")
        assert "--recursive" in bare
        assert bare == after_path  # consistent before and after the first path

    def test_typing_a_path_offers_nothing(self) -> None:
        # While typing a (non-file://) positional there is nothing to complete -
        # no spurious option menu (S3 server-side completion is out of scope).
        assert _names("cp s3://sou") == []

    def test_bare_file_url_still_defers_to_path_completer(self, tmp_path: Path) -> None:
        # The first positional is a bare file:// path (no preceding option): the
        # engine must still defer to FilePathCompleter, not swallow it as an
        # option-name completion.
        import os

        (tmp_path / "alpha.txt").write_text("")
        line = f"{ROOT} cp file://{tmp_path}{os.sep}al"
        results = _completer().autocomplete(line, len(line))
        assert [r.display_text for r in results] == ["alpha.txt"]


# --------------------------------------------------------------------------- #
# Drift guard: the model faithfully reflects the argparse parser              #
# --------------------------------------------------------------------------- #


def _argparse_options(parser: argparse.ArgumentParser) -> set[str]:
    """Every option string a parser declares, minus the -h/--help action."""
    actions = parser._actions  # pyright: ignore[reportPrivateUsage]
    return {
        opt
        for action in actions
        if action.option_strings and "--help" not in action.option_strings
        for opt in action.option_strings
    }


class TestModelReflectsParser:
    def test_offered_options_match_argparse_exactly(self) -> None:
        """Each subcommand's completer option set == its argparse option set.

        The model is derived from ``build_parser`` so it cannot drift from the
        dispatch options by construction; this guards the introspection itself
        (a regression in the global/command split, a dropped option, or a
        phantom one). aws-parity of the option *set* is inherited from the
        ARG_TABLE mirroring the dispatch parser already enforces.
        """
        parser = cli.build_parser()
        for name, subparser in subparser_map(parser).items():
            # The subparser carries its own options plus the shared globals; the
            # completer offers exactly that union (engine level keeps the
            # auto-prompt overrides - only the prompt_toolkit adapter drops them).
            offered = {n for n in _names(f"{name} --") if n.startswith("--")}
            assert offered == _argparse_options(subparser), name


class TestIntNargsParsing:
    """`_consume_value`'s integer-nargs branch (`mb --tags KEY VALUE`) - a
    deliberate improvement over aws's parser, whose fall-through binds KEY to
    the path positional while VALUE is still owed (docs/autoprompt.md
    section 2). Do not "re-align" these to the aws behavior."""

    def test_owed_value_keeps_the_param_context(self) -> None:
        result = CLIParser(build_model()).parse(f"{ROOT} mb --tags k ")
        assert result.current_param == "tags"
        assert result.parsed_params["tags"] == ["k"]
        assert "path" not in result.parsed_params  # KEY did not leak into it

    def test_complete_pair_frees_the_positional(self) -> None:
        result = CLIParser(build_model()).parse(f"{ROOT} mb --tags k v s3://bucket ")
        assert result.current_param is None
        assert result.parsed_params["tags"] == ["k", "v"]
        assert result.parsed_params["path"] == "s3://bucket"
        assert result.unparsed_items == []


# --------------------------------------------------------------------------- #
# cli.main wiring                                                             #
# --------------------------------------------------------------------------- #


class _FakePrompter(AutoPrompter):
    """Records the seed it was handed and returns a canned completed argv."""

    def __init__(self, result: list[str]) -> None:
        self._result = result
        self.seen: list[str] | None = None

    def prompt_for_args(self, argv: list[str]) -> list[str]:
        self.seen = list(argv)
        return self._result


class TestAutoPromptWiring:
    def test_mutual_exclusion_is_rejected(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(["--cli-auto-prompt", "--no-cli-auto-prompt", "ls"])
        assert rc == 252
        err = capsys.readouterr().err
        assert "An error occurred (ParamValidation):" in err
        assert "cannot be specified at the same time" in err

    def test_missing_prompt_toolkit_rejects_with_install_hint(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # prompt_toolkit is present in the test env (dev group); simulate its
        # absence so the degradation path is covered deterministically.
        def _no_spec(name: str, package: str | None = None) -> None:
            return None

        monkeypatch.setattr(importlib.util, "find_spec", _no_spec)
        rc = cli.main(["--cli-auto-prompt", "ls", "s3://b"])
        err = capsys.readouterr().err
        assert rc == 252
        assert "prompt_toolkit" in err
        assert "boto3-s3-cli[autoprompt]" in err

    def test_injected_prompter_receives_seed_and_redispatches(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        prompter = _FakePrompter(["--version"])
        rc = cli.main(["--cli-auto-prompt", "cp", "s3://a"], ctx=Context(auto_prompter=prompter))
        assert rc == 0
        assert prompter.seen == ["cp", "s3://a"]  # seed = typed argv minus the flag
        assert "boto3-s3-cli/" in capsys.readouterr().out  # --version re-dispatched

    def test_redispatched_argv_goes_through_normal_validation(self) -> None:
        # The completed argv is parsed normally: an incomplete `cp` is a usage
        # error (252), proving no special-casing on the re-dispatch path.
        prompter = _FakePrompter(["cp"])
        rc = cli.main(["--cli-auto-prompt"], ctx=Context(auto_prompter=prompter))
        assert rc == 252

    def test_help_takes_precedence_over_prompt(self, capsys: pytest.CaptureFixture[str]) -> None:
        prompter = _FakePrompter(["--version"])
        rc = cli.main(["--cli-auto-prompt", "--help"], ctx=Context(auto_prompter=prompter))
        assert rc == 0
        assert prompter.seen is None  # never prompted
        assert "usage:" in capsys.readouterr().out.lower()

    def test_debug_handlers_detach_during_prompt_and_restore(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The on-partial trial dispatch can enable --debug stream handlers
        # before its usage error falls back to the prompt; they must not paint
        # over the prompt screen (aws swaps handlers into its debug panel for
        # the app run), and they must come back for the re-dispatch.
        botocore_logger = logging.getLogger("botocore")
        handler = logging.NullHandler()
        botocore_logger.addHandler(handler)
        during: list[int] = []

        class _Recording(AutoPrompter):
            def prompt_for_args(self, argv: list[str]) -> list[str]:
                during.append(len(botocore_logger.handlers))
                return ["--version"]

        try:
            rc = cli.main(["--cli-auto-prompt", "ls"], ctx=Context(auto_prompter=_Recording()))
            assert rc == 0
            assert during == [0]  # detached while the prompt ran
            assert handler in botocore_logger.handlers  # restored after
        finally:
            botocore_logger.removeHandler(handler)
        capsys.readouterr()  # swallow the re-dispatched --version output

    def test_no_cli_auto_prompt_does_not_trigger_prompt(self) -> None:
        prompter = _FakePrompter(["--version"])
        rc = cli.main(["--no-cli-auto-prompt", "--version"], ctx=Context(auto_prompter=prompter))
        assert rc == 0
        assert prompter.seen is None  # --no-cli-auto-prompt is a plain no-op


class _EmptyLsClient:
    """Minimal fake: every paginator yields one empty page (zero objects)."""

    def get_paginator(self, name: str) -> Any:
        class _Paginator:
            def paginate(self, **kwargs: Any) -> Any:
                return iter([{}])

        return _Paginator()


class TestAutoPromptModeResolution:
    """Phase 2: AWS_CLI_AUTO_PROMPT env / cli_auto_prompt config / on-partial.

    The autouse ``_isolate_auto_prompt`` fixture pins the env var off; each test
    overrides it (setenv / delenv + AWS_CONFIG_FILE) to drive the resolution.
    """

    def test_env_on_prompts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_CLI_AUTO_PROMPT", "on")
        prompter = _FakePrompter(["--version"])
        rc = cli.main(["cp", "s3://a"], ctx=Context(auto_prompter=prompter))
        assert prompter.seen == ["cp", "s3://a"]
        assert rc == 0

    def test_env_off_does_not_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_CLI_AUTO_PROMPT", "off")
        prompter = _FakePrompter(["--version"])
        rc = cli.main(["cp"], ctx=Context(auto_prompter=prompter))  # missing paths -> 252
        assert prompter.seen is None
        assert rc == 252

    def test_env_invalid_value_behaves_as_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_CLI_AUTO_PROMPT", "yes")  # not on/on-partial
        prompter = _FakePrompter(["--version"])
        rc = cli.main(["cp"], ctx=Context(auto_prompter=prompter))
        assert prompter.seen is None
        assert rc == 252

    def test_config_file_default_profile_on(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("AWS_CLI_AUTO_PROMPT", raising=False)
        config = tmp_path / "config"
        config.write_text("[default]\ncli_auto_prompt = on\n")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
        prompter = _FakePrompter(["--version"])
        rc = cli.main(["cp", "s3://a"], ctx=Context(auto_prompter=prompter))
        assert prompter.seen == ["cp", "s3://a"]
        assert rc == 0

    def test_non_utf8_config_does_not_crash(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The auto-prompt mode resolution runs on every command and reads
        # ~/.aws/config; a non-UTF-8 file must be read as "off", not crash with an
        # uncaught UnicodeDecodeError (regression: configparser.read raises it,
        # and it is not a configparser.Error).
        monkeypatch.delenv("AWS_CLI_AUTO_PROMPT", raising=False)
        config = tmp_path / "config"
        config.write_bytes(b"[default]\ncli_auto_prompt = caf\xe9\n")  # latin-1, invalid UTF-8
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
        # A parse-level usage error (invalid choice, rc 252) still runs the
        # upstream auto-prompt config read; it must read the non-UTF-8 file as
        # "off", not raise UnicodeDecodeError. (Using a parse error keeps the
        # assertion off build_client, which reads the same broken config itself.)
        assert cli.main(["no-such-command"]) == 252

    def test_config_file_named_profile_section(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("AWS_CLI_AUTO_PROMPT", raising=False)
        config = tmp_path / "config"
        config.write_text("[default]\ncli_auto_prompt = off\n[profile foo]\ncli_auto_prompt = on\n")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
        # --profile foo reads [profile foo] -> on
        on = _FakePrompter(["--version"])
        assert cli.main(["--profile", "foo", "cp", "s3://a"], ctx=Context(auto_prompter=on)) == 0
        assert on.seen == ["--profile", "foo", "cp", "s3://a"]
        # default profile reads [default] -> off
        off = _FakePrompter(["--version"])
        assert cli.main(["cp"], ctx=Context(auto_prompter=off)) == 252
        assert off.seen is None

    def test_env_takes_precedence_over_config(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        config = tmp_path / "config"
        config.write_text("[default]\ncli_auto_prompt = on\n")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
        monkeypatch.setenv("AWS_CLI_AUTO_PROMPT", "off")  # env off beats config on
        prompter = _FakePrompter(["--version"])
        assert cli.main(["cp"], ctx=Context(auto_prompter=prompter)) == 252
        assert prompter.seen is None

    def test_explicit_flag_beats_env_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_CLI_AUTO_PROMPT", "off")
        prompter = _FakePrompter(["--version"])
        rc = cli.main(["--cli-auto-prompt", "cp"], ctx=Context(auto_prompter=prompter))
        assert prompter.seen == ["cp"]  # explicit flag wins
        assert rc == 0

    def test_no_flag_beats_env_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_CLI_AUTO_PROMPT", "on")
        prompter = _FakePrompter(["--version"])
        rc = cli.main(["--no-cli-auto-prompt", "cp"], ctx=Context(auto_prompter=prompter))
        assert prompter.seen is None  # --no-cli-auto-prompt wins -> off -> 252
        assert rc == 252

    def test_help_beats_env_on(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("AWS_CLI_AUTO_PROMPT", "on")
        prompter = _FakePrompter(["--version"])
        rc = cli.main(["--help"], ctx=Context(auto_prompter=prompter))
        assert prompter.seen is None
        assert rc == 0
        assert "usage:" in capsys.readouterr().out.lower()

    def test_on_partial_runs_valid_command_without_prompting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AWS_CLI_AUTO_PROMPT", "on-partial")
        prompter = _FakePrompter(["--version"])
        ctx = Context(auto_prompter=prompter, client_factory=lambda _a: _EmptyLsClient())
        rc = cli.main(["ls", "s3://bucket/prefix/"], ctx=ctx)
        assert prompter.seen is None  # valid -> ran, never prompted
        assert rc == 1  # zero objects for a key/prefix (not a usage error)

    def test_on_partial_prompts_on_usage_error(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("AWS_CLI_AUTO_PROMPT", "on-partial")
        prompter = _FakePrompter(["--version"])
        rc = cli.main(["cp"], ctx=Context(auto_prompter=prompter))  # missing paths -> 252
        assert prompter.seen == ["cp"]  # fell back to prompting
        assert rc == 0  # re-dispatched --version
        # The trial's usage block is silenced so it doesn't bury the prompt
        # (aws's SilenceParamValidationMsgErrorHandler).
        assert "usage:" not in capsys.readouterr().err.lower()

    def test_config_driven_without_prompt_toolkit_falls_through(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Config/env-driven (not an explicit flag) + no prompt_toolkit and no
        # injected prompter: fall through to normal dispatch instead of breaking
        # the command with an install hint.
        monkeypatch.setenv("AWS_CLI_AUTO_PROMPT", "on")
        monkeypatch.setattr(importlib.util, "find_spec", lambda name, package=None: None)
        rc = cli.main(["no-such-command"])  # dispatch -> argparse usage error
        err = capsys.readouterr().err
        assert rc == 252
        assert "prompt_toolkit" not in err  # the install hint was NOT shown


class TestScopedConfigFileChoice:
    """``_read_scoped_cli_auto_prompt`` is present-wins on ``AWS_CONFIG_FILE``
    (botocore's EnvironmentProvider): an *empty* value means "no config file",
    never a fallback to ``~/.aws/config`` - a scripted run neutralizing the
    config must not get an interactive prompt from the fallback file."""

    def _seed_fallback_config(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # Point expanduser's ~ at tmp_path and plant cli_auto_prompt = on in
        # the fallback location.
        config = tmp_path / ".aws" / "config"
        config.parent.mkdir()
        config.write_text("[default]\ncli_auto_prompt = on\n")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows expanduser reads this, not HOME

    def test_empty_env_value_disables_the_fallback_read(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._seed_fallback_config(monkeypatch, tmp_path)
        monkeypatch.setenv("AWS_CONFIG_FILE", "")
        assert resolve._read_scoped_cli_auto_prompt("default") is None

    def test_unset_env_reads_the_fallback_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._seed_fallback_config(monkeypatch, tmp_path)
        monkeypatch.delenv("AWS_CONFIG_FILE", raising=False)
        assert resolve._read_scoped_cli_auto_prompt("default") == "on"


class TestPromptToolkitAdapter:
    """The prompt_toolkit Completion adapter (skipped without the autoprompt extra)."""

    def _menu(self, buffer: str) -> list[Any]:
        pytest.importorskip("prompt_toolkit")
        from prompt_toolkit.completion import CompleteEvent
        from prompt_toolkit.document import Document

        from boto3_s3_cli.autoprompt.prompt import PromptToolkitCompleter, build_completion_source

        completer = PromptToolkitCompleter(build_completion_source())
        document = Document(text=buffer, cursor_position=len(buffer))
        return list(completer.get_completions(document, CompleteEvent()))

    def test_inserts_subcommand_completions(self) -> None:
        names = [comp.text for comp in self._menu("")]
        assert "cp" in names and "website" in names

    def test_filters_out_auto_prompt_overrides(self) -> None:
        names = [comp.text for comp in self._menu("cp --cli")]
        assert "--cli-auto-prompt" not in names
        assert "--no-cli-auto-prompt" not in names
        assert "--cli-binary-format" in names  # other --cli options stay

    def test_display_meta_carries_type(self) -> None:
        recursive = next(comp for comp in self._menu("cp --recursiv") if comp.text == "--recursive")
        assert "[boolean]" in recursive.display_meta_text

    def test_completer_exception_is_swallowed_but_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A completer bug must never crash the interactive prompt, but it must
        # leave a --debug trail (aws logs the same in its adapter).
        pytest.importorskip("prompt_toolkit")
        from prompt_toolkit.completion import CompleteEvent
        from prompt_toolkit.document import Document

        from boto3_s3_cli.autoprompt.prompt import PromptToolkitCompleter

        class _Boom(c.AutoCompleter):
            def __init__(self) -> None:
                pass

            def autocomplete(
                self, command_line: str, index: int | None = None
            ) -> list[c.CompletionResult]:
                raise RuntimeError("boom")

        completer = PromptToolkitCompleter(_Boom())
        document = Document(text="cp ", cursor_position=3)
        with caplog.at_level(logging.DEBUG, logger="boto3_s3_cli.autoprompt.prompt"):
            results = list(completer.get_completions(document, CompleteEvent()))
        assert results == []  # swallowed, no crash
        assert any(
            record.levelno == logging.DEBUG and "get_completions" in record.getMessage()
            for record in caplog.records
        )
