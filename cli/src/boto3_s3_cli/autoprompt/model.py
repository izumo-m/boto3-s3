"""The completion model: the command/option surface the auto-prompt completes.

aws-cli drives auto-completion from a prebuilt SQLite index of its command
table. We derive the equivalent from our own ``argparse`` parser
(``boto3_s3_cli.cli.build_parser``) so there is a single source of truth for
the option set - the same definitions that dispatch the commands also feed
completion, and the two cannot drift. ``CompletionModel`` exposes the small
query surface the ported ``CLIParser`` and
completers need (the methods aws-cli's ``ModelIndex`` provides).

Structure note: aws's hierarchy is ``aws s3 <sub>`` (three levels); ours is
``boto3-s3 <sub>`` (two). The parser normalizes the executable token to
``ROOT``, so globals live under ``command_name == ROOT`` and the subcommands
hang directly off ``lineage == [ROOT]``.

Pure Python - no ``prompt_toolkit``, no AWS SDK - so the whole completion
pipeline is importable and testable without the optional extra.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import cast

# The executable token the parser normalizes to (aws-cli uses 'aws'). Globals
# are keyed under this name; the subcommands are its children.
ROOT = "boto3-s3"


@dataclass(frozen=True)
class ArgData:
    """Metadata for one option or positional, mirroring aws-cli's ``CLIArgument``.

    ``type_name`` is ``"boolean"`` for flags that take no value (so the parser
    knows not to consume one) and ``"string"`` otherwise - that is the only
    distinction the parser acts on. ``choices`` / ``help_text`` are ours, used
    by the value/name completers; aws keeps the analogous data on its argument
    model. ``name`` is the hyphenated CLI spelling without the ``--`` (e.g.
    ``"page-size"``), matching how the parser keys params.
    """

    name: str
    type_name: str
    nargs: int | str | None
    required: bool
    choices: tuple[str, ...] | None
    help_text: str | None
    positional: bool


@dataclass(frozen=True)
class CommandData:
    """One subcommand's completable surface (its own options + positionals)."""

    help_text: str
    options: dict[str, ArgData]
    positionals: tuple[ArgData, ...]


class CompletionModel:
    """The query surface the ported parser and completers consume.

    Methods mirror the names/semantics of aws-cli's ``ModelIndex`` so the port
    stays faithful: ``command_names``, ``arg_names``,
    ``get_argument_data``, plus ``global_arg_data`` (aws-cli's
    ``get_global_arg_data``, shortened) for the option-name
    completer's global injection.
    """

    def __init__(self, globals_: dict[str, ArgData], commands: dict[str, CommandData]) -> None:
        self._globals = globals_
        self._commands = commands

    def command_names(self, lineage: list[str]) -> list[str]:
        # Subcommands hang off [ROOT]; nothing nests deeper.
        if lineage == [ROOT]:
            return list(self._commands)
        return []

    def commands_with_full_name(self, lineage: list[str]) -> list[tuple[str, str]]:
        if lineage == [ROOT]:
            return [(name, cmd.help_text) for name, cmd in self._commands.items()]
        return []

    def arg_names(
        self, lineage: list[str], command_name: str | None, positional_arg: bool = False
    ) -> list[str]:
        if command_name == ROOT:
            return [] if positional_arg else list(self._globals)
        command = self._commands.get(command_name or "")
        if command is None:
            return []
        if positional_arg:
            return [arg.name for arg in command.positionals]
        return list(command.options)

    def get_argument_data(
        self, lineage: list[str], command_name: str | None, arg_name: str
    ) -> ArgData | None:
        if command_name == ROOT:
            return self._globals.get(arg_name)
        command = self._commands.get(command_name or "")
        if command is None:
            return None
        if arg_name in command.options:
            return command.options[arg_name]
        for positional in command.positionals:
            if positional.name == arg_name:
                return positional
        return None

    def global_arg_data(self) -> list[ArgData]:
        return list(self._globals.values())


def _is_help(action: argparse.Action) -> bool:
    # Our CLI exposes -h/--help (argparse) but no `help` subcommand; aws-cli is
    # the reverse (a `help` subcommand, no --help global). Neither tool's help
    # mechanism is in the other's candidate set, and help is charter-exempt
    # (overview.md section 3 exception 1), so it is excluded from completion entirely.
    return "--help" in action.option_strings or "-h" in action.option_strings


def _arg_from_action(action: argparse.Action) -> ArgData:
    # argparse sets nargs=0 for store_true/store_false and our --version action;
    # those consume no value, so the parser must see them as 'boolean'.
    type_name = "boolean" if action.nargs == 0 else "string"
    name = next((opt[2:] for opt in action.option_strings if opt.startswith("--")), action.dest)
    choices = tuple(str(c) for c in action.choices) if action.choices else None
    return ArgData(
        name=name,
        type_name=type_name,
        nargs=action.nargs,
        required=bool(action.required),
        choices=choices,
        help_text=action.help,
        positional=not action.option_strings,
    )


def _actions(parser: argparse.ArgumentParser) -> list[argparse.Action]:
    return parser._actions  # pyright: ignore[reportPrivateUsage]


def subparser_map(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    """The subcommand-name -> subparser map, found via the subparsers action."""
    for action in _actions(parser):
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict):
            members = cast("dict[object, object]", choices)
            if all(isinstance(p, argparse.ArgumentParser) for p in members.values()):
                return cast("dict[str, argparse.ArgumentParser]", choices)
    raise RuntimeError("boto3-s3 parser has no subparsers")  # pragma: no cover


def build_model() -> CompletionModel:
    """Introspect ``boto3_s3_cli.cli.build_parser`` into a completion model.

    Globals are the top-level parser's options (added to every subparser via the
    shared parent, so they are filtered out of each subcommand's own option set
    by spelling). Imported lazily to keep the module SDK-free and avoid an import
    cycle - only reached on the auto-prompt path.
    """
    from boto3_s3_cli.cli import build_parser

    parser = build_parser()

    # The subparsers action is the top parser's only positional, so the
    # `not option_strings` guard skips it along with any positional; globals are
    # exactly the top-level options minus help.
    global_option_strings: set[str] = set()
    globals_: dict[str, ArgData] = {}
    for action in _actions(parser):
        if _is_help(action) or not action.option_strings:
            continue
        global_option_strings.update(action.option_strings)
        arg = _arg_from_action(action)
        globals_[arg.name] = arg

    commands: dict[str, CommandData] = {}
    for name, subparser in subparser_map(parser).items():
        options: dict[str, ArgData] = {}
        positionals: list[ArgData] = []
        for action in _actions(subparser):
            if _is_help(action):
                continue
            if not action.option_strings:
                positionals.append(_arg_from_action(action))
            elif not set(action.option_strings) & global_option_strings:
                arg = _arg_from_action(action)
                options[arg.name] = arg
        commands[name] = CommandData(
            help_text=subparser.description or "",
            options=options,
            positionals=tuple(positionals),
        )

    return CompletionModel(globals_, commands)
