"""Partial command-line parser for completion - a port of aws-cli's ``CLIParser``.

aws-cli's ``awscli/autocomplete/parser.py``. Unlike the dispatch parser
(``argparse``), this one tolerates incomplete input: it parses everything it
understands and records the trailing fragment to complete, never erroring. It is
command-agnostic; the command/option knowledge comes from the injected
``CompletionModel``.

Adapted from the aws-cli source in two ways. First the root token: aws normalizes
the executable to ``'aws'`` and nests services under it; we normalize to
``ROOT`` with the subcommands directly
beneath. Second, ``_handle_positional`` is tuned for completion *usability* over
a byte-faithful port (the auto-prompt UI is charter-exempt - ``docs/autoprompt.md``
section 2): a value typed for an option before any positional keeps ``current_param`` on
that option, and a token after a filled positional slot (cp/mv/sync take two
paths) keeps the command's options live instead of dropping into
``unparsed_items``. Pure Python (no ``prompt_toolkit``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from boto3_s3_cli.autoprompt.model import ROOT

if TYPE_CHECKING:
    from boto3_s3_cli.autoprompt.model import CompletionModel

WORD_BOUNDARY = ""


class ParsedResult:
    """The outcome of parsing a partial command line (see aws-cli docstring).

    ``current_command`` is the leaf subcommand; ``current_param`` is the option
    whose value is currently being typed (or ``None``); ``current_fragment`` is
    the trailing partial token to complete; ``lineage`` is the command path
    above the leaf; ``unparsed_items`` are tokens the parser did not recognize.
    """

    def __init__(
        self,
        current_command: str | None = None,
        current_param: str | None = None,
        global_params: dict[str, object] | None = None,
        parsed_params: dict[str, object] | None = None,
        lineage: list[str] | None = None,
        current_fragment: str | None = None,
        unparsed_items: list[str] | None = None,
    ) -> None:
        self.current_command = current_command
        self.current_param = current_param
        self.global_params: dict[str, object] = {} if global_params is None else global_params
        self.parsed_params: dict[str, object] = {} if parsed_params is None else parsed_params
        self.lineage: list[str] = [] if lineage is None else lineage
        self.current_fragment = current_fragment
        self.unparsed_items: list[str] = [] if unparsed_items is None else unparsed_items

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ParsedResult):
            return False
        return self.__dict__ == other.__dict__


class _ParseState:
    """Mutable parse cursor; pushing a new command demotes the old to lineage."""

    def __init__(self) -> None:
        self._current_command: str | None = None
        self.current_param: str | None = None
        self._lineage: list[str] = []

    @property
    def current_command(self) -> str | None:
        return self._current_command

    @current_command.setter
    def current_command(self, value: str | None) -> None:
        if self._current_command is not None:
            self._lineage.append(self._current_command)
        self._current_command = value

    @property
    def lineage(self) -> list[str]:
        return self._lineage

    @property
    def full_lineage(self) -> list[str]:
        if self.current_command is None:
            return self._lineage
        return [*self._lineage, self.current_command]


class CLIParser:
    """Parses a partial ``boto3-s3`` command line for completion."""

    def __init__(self, model: CompletionModel) -> None:
        self._index = model

    def parse(self, command_line: str, location: int | None = None) -> ParsedResult:
        """Parse the command prefix up to the cursor into completion context."""
        # NOTE (carried from aws-cli): `--foo=bar` is not supported as a separator.
        parsed = ParsedResult()
        state, remaining_parts = self._split_to_parts(command_line, location)
        global_args = self._index.arg_names(lineage=[], command_name=ROOT)
        current_args: list[str] = []
        while remaining_parts:
            current = remaining_parts.pop(0)
            if current.startswith("--"):
                self._handle_option(
                    current, remaining_parts, current_args, global_args, parsed, state
                )
            else:
                current_args = self._handle_positional(current, state, remaining_parts, parsed)
        parsed.current_command = state.current_command
        parsed.current_param = state.current_param
        parsed.lineage = state.lineage
        return parsed

    def _consume_value(
        self,
        remaining_parts: list[str],
        option_name: str,
        lineage: list[str],
        current_command: str | None,
        state: _ParseState,
    ) -> object:
        """Consume an option value according to the completion model's `nargs`."""
        arg_data = self._index.get_argument_data(
            lineage=lineage, command_name=current_command, arg_name=option_name
        )
        if arg_data is not None and arg_data.type_name == "boolean":
            state.current_param = None
            return True
        elif remaining_parts == [WORD_BOUNDARY]:
            return ""
        elif len(remaining_parts) <= 1:
            return None
        nargs = arg_data.nargs if arg_data is not None else None
        if nargs is None:
            result = remaining_parts.pop(0)
            state.current_param = None
            return result
        elif nargs == "?":
            if remaining_parts and not remaining_parts[0].startswith("--"):
                result = remaining_parts.pop(0)
                state.current_param = None
                return result
        elif isinstance(nargs, str) and nargs in "+*":
            value: list[str] = []
            while len(remaining_parts) > 0 and not remaining_parts == [WORD_BOUNDARY]:
                if remaining_parts[0].startswith("--"):
                    state.current_param = None
                    break
                if len(remaining_parts) == 1:
                    break
                value.append(remaining_parts.pop(0))
            return value
        return None

    def _split_to_parts(
        self, command_line: str, location: int | None
    ) -> tuple[_ParseState, list[str]]:
        state = _ParseState()
        if location is not None:
            command_line = command_line[:location]
        parts = command_line.split()
        if command_line and command_line[-1].isspace():
            # Trailing space => the last word is complete; append a boundary so it
            # is not treated as a fragment ("stop-<TAB>" completes, "stop- <TAB>"
            # does not).
            parts.append(WORD_BOUNDARY)
        if parts:
            # Drop the executable token and normalize it to ROOT.
            parts.pop(0)
            state.current_command = ROOT
        return state, parts

    def _handle_option(
        self,
        current: str,
        remaining_parts: list[str],
        current_args: list[str],
        global_args: list[str],
        parsed: ParsedResult,
        state: _ParseState,
    ) -> None:
        """Record a known option or preserve an incomplete/unknown token."""
        option_name = current[2:]
        if option_name in global_args:
            state.current_param = option_name
            parsed.global_params[option_name] = self._consume_value(
                remaining_parts, option_name, lineage=[], current_command=ROOT, state=state
            )
        elif option_name in current_args:
            state.current_param = option_name
            parsed.parsed_params[option_name] = self._consume_value(
                remaining_parts, option_name, state.lineage, state.current_command, state
            )
        elif self._is_last_word(remaining_parts, current):
            parsed.current_fragment = current
        else:
            parsed.unparsed_items.append(current)

    def _is_last_word(self, remaining_parts: list[str], current: str) -> bool:
        return not remaining_parts and bool(current)

    def _is_part_of_command(self, current: str, command_names: list[str]) -> bool:
        return any(command.startswith(current) and command != current for command in command_names)

    def _is_command_name(
        self, current: str, remaining_parts: list[str], command_names: list[str]
    ) -> bool:
        # 'current' is a command if it is a known name AND either we've moved past
        # it (more parts follow) or no other command merely starts with it.
        is_command_name = current in command_names
        is_part_of_command = self._is_part_of_command(current, command_names)
        return is_command_name and (bool(remaining_parts) or not is_part_of_command)

    def _handle_positional(
        self,
        current: str,
        state: _ParseState,
        remaining_parts: list[str],
        parsed: ParsedResult,
    ) -> list[str]:
        """Advance command lineage or bind a positional while keeping options live."""
        command_names = self._index.command_names(state.full_lineage)
        positional_argname = None
        if self._is_command_name(current, remaining_parts, command_names):
            state.current_command = current
            return self._index.arg_names(lineage=state.lineage, command_name=state.current_command)
        if not command_names:
            positional_argname = self._get_positional_argname(state)
        if positional_argname and positional_argname not in parsed.parsed_params:
            if not remaining_parts:
                parsed.current_fragment = current
                # Claim the positional only for a non-empty fragment with no
                # option mid-value. A value typed for a preceding option
                # (`cp --storage-class <TAB>` before any path) must keep
                # current_param on that option so its value completer fires (the
                # port reset it to the positional and suppressed the value). A
                # real positional fragment being typed (`cp file://x`) does claim
                # it, so ModelIndexCompleter defers to FilePathCompleter. The
                # empty boundary (`cp <TAB>`) claims nothing, so the option set is
                # offered - matching `cp src <TAB>` after the first path.
                # (Usability tuning, not in the faithful port - autoprompt.md section 2.)
                if state.current_param is None and current:
                    state.current_param = positional_argname
            elif current:
                parsed.parsed_params[positional_argname] = current
                state.current_param = None
            return self._index.arg_names(lineage=state.lineage, command_name=state.current_command)
        else:
            if not remaining_parts:
                parsed.current_fragment = current
            elif current:
                parsed.unparsed_items.append(current)
            # Keep the command's options live. The port returned None here, which
            # nulled current_args, so any option after a *second* positional path
            # (cp/mv/sync take two) fell into unparsed_items and stopped
            # completing. Returning the option set lets those options parse
            # normally - name completion, value completion, and dedup all work.
            # (Usability tuning - docs/autoprompt.md section 2.)
            return self._index.arg_names(lineage=state.lineage, command_name=state.current_command)

    def _get_positional_argname(self, state: _ParseState) -> str | None:
        positional_args = self._index.arg_names(
            lineage=state.lineage, command_name=state.current_command, positional_arg=True
        )
        # We assume at most one positional per command (true for `aws s3`).
        return positional_args[0] if positional_args else None
