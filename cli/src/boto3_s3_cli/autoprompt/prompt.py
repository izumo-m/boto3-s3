"""The ``prompt_toolkit``-backed auto-prompt - the only module that binds it.

Imported only when ``--cli-auto-prompt`` fires on an install that has the
``autoprompt`` extra. ``PromptToolkitCompleter`` adapts our pure-Python
``AutoCompleter`` to ``prompt_toolkit``
(a port of aws-cli's ``awscli/autoprompt/prompttoolkit.py``'s adapter), and
``PromptToolkitAutoPrompter`` runs the editable prompt and returns the
edited argv. We use ``prompt_toolkit``'s standard completion menu rather than
cloning aws's full-screen doc-panel app - the contract is candidate parity, not
UI chrome (console output is non-contractual, option-handling section 6).
"""

from __future__ import annotations

import logging
import shlex
import sys
from typing import TYPE_CHECKING

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion, ThreadedCompleter

from boto3_s3_cli.autoprompt import completers as completers_mod
from boto3_s3_cli.autoprompt.completers import AutoCompleter
from boto3_s3_cli.autoprompt.model import ROOT, build_model
from boto3_s3_cli.autoprompt.parser import CLIParser
from boto3_s3_cli.autoprompt.prompter import AutoPrompter

if TYPE_CHECKING:
    from collections.abc import Iterable

    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document

    from boto3_s3_cli.autoprompt.completers import CompletionResult

logger = logging.getLogger(__name__)

_AUTO_PROMPT_OVERRIDES = ("--cli-auto-prompt", "--no-cli-auto-prompt")


def build_completion_source() -> AutoCompleter:
    """Assemble the completer chain (order matters: first non-None wins)."""
    model = build_model()
    parser = CLIParser(model)
    fuzzy = completers_mod.fuzzy_filter
    return AutoCompleter(
        parser,
        [
            completers_mod.RegionCompleter(fuzzy),
            completers_mod.ProfileCompleter(fuzzy),
            completers_mod.ModelIndexCompleter(model, fuzzy),
            completers_mod.FilePathCompleter(fuzzy),
            completers_mod.ChoicesCompleter(model, fuzzy),
        ],
    )


class PromptToolkitCompleter(Completer):
    """Converts our ``CompletionResult``s into ``prompt_toolkit`` completions."""

    def __init__(self, completion_source: AutoCompleter) -> None:
        self._source = completion_source

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        try:
            text_before_cursor = document.text_before_cursor
            # The buffer holds only what follows the `ROOT ` prompt prefix; the
            # parser expects the executable token, so prepend it (aws prepends
            # 'aws ' the same way).
            text = f"{ROOT} {text_before_cursor}"
            low_level = self._source.autocomplete(text, len(text))
            yield from self._convert(low_level, text_before_cursor)
        except Exception:
            # Swallow so a completer bug never kills the interactive prompt
            # (aws's adapter does the same). The debug record is emitted for
            # completeness but stays invisible while the prompt owns the
            # terminal: the CLI detaches its --debug handlers around the prompt
            # and has no aws-style debug panel to route records into.
            logger.debug("exception in PromptToolkitCompleter.get_completions", exc_info=True)
            return

    def _convert(
        self, low_level: list[CompletionResult], text_before_cursor: str
    ) -> Iterable[Completion]:
        word_before_cursor = self._strip_whitespace(text_before_cursor)
        unique = self._dedupe(self._drop_overrides(low_level))
        location = self._starting_location(text_before_cursor, word_before_cursor)
        for completion in self._required_first(unique):
            yield Completion(
                completion.name,
                location,
                display=self._display_text(completion),
                display_meta=self._display_meta(completion),
            )

    def _strip_whitespace(self, text: str) -> str:
        return text.strip().split()[-1] if text.strip() else ""

    def _drop_overrides(self, completions: list[CompletionResult]) -> list[CompletionResult]:
        return [c for c in completions if c.name not in _AUTO_PROMPT_OVERRIDES]

    def _dedupe(self, completions: list[CompletionResult]) -> list[CompletionResult]:
        seen: set[str] = set()
        unique: list[CompletionResult] = []
        for completion in completions:
            if completion.name not in seen:
                seen.add(completion.name)
                unique.append(completion)
        return unique

    def _required_first(self, completions: list[CompletionResult]) -> list[CompletionResult]:
        required = [c for c in completions if c.required]
        optional = [c for c in completions if not c.required]
        return required + optional

    def _display_text(self, completion: CompletionResult) -> str:
        if completion.display_text is not None:
            return completion.display_text
        if completion.name.startswith("--") and completion.required:
            return f"{completion.name} (required)"
        return completion.name

    def _display_meta(self, completion: CompletionResult) -> str:
        meta = ""
        if completion.cli_type_name:
            meta += f"[{completion.cli_type_name}] "
        if completion.help_text:
            meta += completion.help_text
        return meta

    def _starting_location(self, text_before_cursor: str, word_before_cursor: str) -> int:
        if text_before_cursor and text_before_cursor[-1] == " ":
            return 0
        return -len(word_before_cursor)


class PromptToolkitAutoPrompter(AutoPrompter):
    """Runs an editable prompt seeded with the partial command; returns the argv."""

    def __init__(self, completion_source: AutoCompleter | None = None) -> None:
        self._source = (
            completion_source if completion_source is not None else build_completion_source()
        )

    def prompt_for_args(self, argv: list[str]) -> list[str]:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise RuntimeError("--cli-auto-prompt requires an interactive terminal.")
        # ThreadedCompleter, like aws's adapter: with complete_while_typing,
        # candidate generation otherwise runs inside the event loop and every
        # keystroke blocks on it (the first --region/--profile completion pays
        # a boto3 session load; FilePathCompleter lists directories). The only
        # mutable state the completion thread reaches is the Region/Profile
        # completers' caches, where an overlapping run merely recomputes the
        # same list.
        session: PromptSession[str] = PromptSession(
            completer=ThreadedCompleter(PromptToolkitCompleter(self._source)),
            complete_while_typing=True,
        )
        text = session.prompt(f"{ROOT} ", default=shlex.join(argv))
        return shlex.split(text)


def build_default_prompter() -> AutoPrompter:
    return PromptToolkitAutoPrompter()
