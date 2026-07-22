"""The completers and the filter/combiner - a port of aws-cli's completion core.

Ports ``CompletionResult`` / ``AutoCompleter`` (``completer.py``),
``fuzzy_filter`` (``filters.py``), and the completers from
``autocomplete/local/basic.py``, scoped to what ``aws s3`` actually exercises:

- ``ModelIndexCompleter`` - subcommand names and option names.
- ``RegionCompleter`` / ``ProfileCompleter`` - ``--region`` /
  ``--profile`` values (local botocore/config data).
- ``FilePathCompleter`` - ``file://`` / ``fileb://`` paths.
- ``ChoicesCompleter`` - values for any option with fixed ``choices``.

The auto-prompt UI is charter-exempt (``docs/autoprompt.md`` section 1), so these
completers favor *usability* over a byte-faithful port. ``ChoicesCompleter``
completes every choice-bearing option uniformly from the argparse model, where
aws only completes *global* choices (its command-level path reads
``argument_model.enum``, empty for the ``s3`` customization args whose values
live on ``.choices``) - so ``--storage-class`` etc. complete here but not in aws.
Reachability is widened the same way by the parser tuning (``parser.py``): option
values complete before any positional, and options keep completing after a
command's second positional path.

Pure Python - boto3 is imported lazily, only when a region/profile value is
actually being completed (the interactive path), never on a parse path.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple

from boto3_s3_cli.autoprompt.model import ROOT

if TYPE_CHECKING:
    from boto3_s3_cli.autoprompt.model import CompletionModel
    from boto3_s3_cli.autoprompt.parser import CLIParser, ParsedResult


@dataclass
class CompletionResult:
    """One completion candidate plus the metadata the prompt UI renders.

    Mirrors aws-cli's ``CompletionResult``: ``name`` is inserted, ``display_text``
    overrides the menu label, ``cli_type_name`` / ``help_text`` feed the
    ``display_meta`` line, ``required`` drives required-first ordering.
    """

    name: str
    starting_index: int = 0
    required: bool = False
    cli_type_name: str = ""
    help_text: str | None = None
    display_text: str | None = None


# A response filter narrows + ranks candidates against the current fragment;
# auto-prompt uses fuzzy_filter (aws-cli's non-auto-prompt shell completion
# defaults to a startswith filter, a path this port does not implement).
ResponseFilter = Callable[[str, "list[CompletionResult]"], "list[CompletionResult]"]
# A value provider sources region/profile candidates; injecting a fake keeps
# candidate sourcing off botocore and the network (the surrounding tests still
# import botocore transitively through the command model build).
ValueProvider = Callable[[], Iterable[str]]


class _FuzzyMatch(NamedTuple):
    match_length: int
    start_pos: int
    completion: CompletionResult


def fuzzy_filter(prefix: str, completions: list[CompletionResult]) -> list[CompletionResult]:
    """Subsequence match + rank, exactly as aws-cli's auto-prompt filter.

    ``"rmt"`` becomes ``/r.*?m.*?t/``; each candidate keeps its left-most, then
    shortest match, and results sort by (match length, start, display, name).
    One guard beyond aws: a missing ``display_text`` sorts as ``""`` where aws
    sorts the raw ``None`` (which cannot mix with strings), so ties between
    display and non-display candidates may order differently there.
    """
    if prefix and completions:
        fuzzy_matches: list[_FuzzyMatch] = []
        pattern = ".*?".join(map(re.escape, prefix))
        pattern = f"(?=({pattern}))"
        regex = re.compile(pattern, re.IGNORECASE)
        for completion in completions:
            name = completion.display_text or completion.name
            matches = list(regex.finditer(name))
            if matches:
                best = min(matches, key=lambda m: (m.start(), len(m.group(1))))
                fuzzy_matches.append(_FuzzyMatch(len(best.group(1)), best.start(), completion))
        return [
            match.completion
            for match in sorted(
                fuzzy_matches,
                key=lambda m: (
                    m.match_length,
                    m.start_pos,
                    m.completion.display_text or "",
                    m.completion.name,
                ),
            )
        ]
    return completions


def _clean_help(text: str | None) -> str | None:
    """Strip HTML tags and flatten newlines - aws-cli's ``strip_html_tags_and_newlines``.

    aws deletes newlines (``.replace("\\n", "")``); we replace them with a space
    so adjacent words don't fuse in the single-line ``display_meta`` label.
    """
    if not text:
        return None
    return re.sub("<.*?>", "", text).replace("\n", " ")


class BaseCompleter:
    """Return ``None`` to defer to the next completer, or a (possibly empty) list."""

    def complete(self, parsed: ParsedResult) -> list[CompletionResult] | None:
        raise NotImplementedError("complete")


class AutoCompleter:
    """Runs completers in order; the first non-``None`` result wins (aws-faithful)."""

    def __init__(self, parser: CLIParser, completers: list[BaseCompleter]) -> None:
        self._parser = parser
        self._completers = completers

    def autocomplete(self, command_line: str, index: int | None = None) -> list[CompletionResult]:
        parsed = self._parser.parse(command_line, index)
        for completer in self._completers:
            result = completer.complete(parsed)
            if result is not None:
                return result
        return []


class ModelIndexCompleter(BaseCompleter):
    """Completes subcommand names and option names from the model."""

    def __init__(self, model: CompletionModel, response_filter: ResponseFilter) -> None:
        self._index = model
        self._filter = response_filter

    def complete(self, parsed: ParsedResult) -> list[CompletionResult] | None:
        # cp/mv/sync take two positional paths but the model has one slot, so a
        # second positional lands in unparsed_items. Offer the option set there
        # whatever it looks like - usability first (the auto-prompt UI is
        # charter-exempt, docs/autoprompt.md section 2/3): aws drops a bare
        # `outdir` on a path-likeness heuristic, we keep completing, and we never
        # offer less than aws does. We defer only while an option *value* is being
        # typed (current_param), so its value completer fires instead.
        if parsed.unparsed_items and not parsed.current_param:
            # Empty trailing fragment -> the whole set; a `--frag` narrows it.
            fragment = parsed.current_fragment or ""
            prefix = "" if fragment == "--" else fragment
            return self._filter(prefix, self._complete_options(parsed))
        elif parsed.unparsed_items or parsed.current_fragment is None or parsed.current_param:
            # Nothing being typed, or an option value: defer (value completers
            # handle the last case).
            return None
        current_fragment = parsed.current_fragment
        if current_fragment.startswith("--"):
            prefix = "" if current_fragment == "--" else current_fragment
            return self._filter(prefix, self._complete_options(parsed))
        commands = self._complete_command(parsed)
        if not commands:
            return self._filter(current_fragment, self._complete_options(parsed))
        return self._filter(current_fragment, commands)

    def _complete_command(self, parsed: ParsedResult) -> list[CompletionResult]:
        lineage = parsed.lineage + ([parsed.current_command] if parsed.current_command else [])
        offset = -len(parsed.current_fragment or "")
        return [
            CompletionResult(name, help_text=full_name, starting_index=offset)
            for name, full_name in self._index.commands_with_full_name(lineage)
        ]

    def _complete_options(self, parsed: ParsedResult) -> list[CompletionResult]:
        offset = -len(parsed.current_fragment or "")
        is_in_global_scope = parsed.lineage == [] and parsed.current_command == ROOT
        results: list[CompletionResult] = []
        if not is_in_global_scope:
            for arg_name in self._index.arg_names(
                lineage=parsed.lineage, command_name=parsed.current_command
            ):
                arg = self._index.get_argument_data(
                    parsed.lineage, parsed.current_command, arg_name
                )
                if arg is None:
                    continue
                results.append(
                    CompletionResult(
                        f"--{arg_name}",
                        starting_index=offset,
                        required=arg.required,
                        cli_type_name=arg.type_name,
                        help_text=_clean_help(arg.help_text),
                    )
                )
        for arg in self._index.global_arg_data():
            results.append(
                CompletionResult(
                    f"--{arg.name}",
                    starting_index=offset,
                    required=False,
                    cli_type_name=arg.type_name,
                    help_text=_clean_help(arg.help_text),
                )
            )
        supplied = list(parsed.parsed_params) + list(parsed.global_params)
        return [r for r in results if r.name.strip("-") not in supplied]


class RegionCompleter(BaseCompleter):
    """Completes ``--region`` values from local botocore endpoint data (no network)."""

    def __init__(
        self, response_filter: ResponseFilter, regions_provider: ValueProvider | None = None
    ) -> None:
        self._filter = response_filter
        self._regions_provider = regions_provider
        self._cache: list[str] | None = None

    def complete(self, parsed: ParsedResult) -> list[CompletionResult] | None:
        if parsed.current_param == "region" and parsed.current_fragment is not None:
            results = [CompletionResult(name=r) for r in self._regions()]
            return self._filter(parsed.current_fragment, results)
        return None

    def _regions(self) -> list[str]:
        # Cached for the completer's lifetime (one interactive prompt session):
        # value completion fires on every keystroke, and a fresh
        # boto3.Session().get_available_regions() costs ~40 ms each (vs ~3 ms once
        # resolved). aws caches the session for the same reason (it reuses one
        # botocore Session across completions - autocomplete/local/basic.py).
        if self._cache is None:
            if self._regions_provider is not None:
                self._cache = list(self._regions_provider())
            else:
                import boto3  # lazy: only on an actual --region completion

                # We are an S3-only CLI, so always resolve S3's regions (aws keys
                # off the service in the command lineage, which for us is always s3).
                self._cache = boto3.Session().get_available_regions("s3")
        return self._cache


class ProfileCompleter(BaseCompleter):
    """Completes ``--profile`` values from the local AWS config (no network)."""

    def __init__(
        self, response_filter: ResponseFilter, profiles_provider: ValueProvider | None = None
    ) -> None:
        self._filter = response_filter
        self._profiles_provider = profiles_provider
        self._cache: list[str] | None = None

    def complete(self, parsed: ParsedResult) -> list[CompletionResult] | None:
        if parsed.current_param == "profile" and parsed.current_fragment is not None:
            results = [CompletionResult(name=p) for p in self._profiles()]
            return self._filter(parsed.current_fragment, results)
        return None

    def _profiles(self) -> list[str]:
        # Cached for the prompt session, like the regions above (aws-faithful).
        if self._cache is None:
            if self._profiles_provider is not None:
                self._cache = list(self._profiles_provider())
            else:
                import boto3  # lazy: only on an actual --profile completion

                self._cache = boto3.Session().available_profiles
        return self._cache


class FilePathCompleter(BaseCompleter):
    """Completes local paths once the fragment starts with ``file://``/``fileb://``.

    aws drives this with ``prompt_toolkit``'s ``PathCompleter``; we reimplement
    the directory listing in pure Python so the completion engine carries no
    ``prompt_toolkit`` dependency (only ``prompt`` binds it).
    """

    _PREFIXES = ("file://", "fileb://")

    def __init__(self, response_filter: ResponseFilter) -> None:
        self._filter = response_filter

    def complete(self, parsed: ParsedResult) -> list[CompletionResult] | None:
        fragment = parsed.current_fragment
        if not fragment or not fragment.startswith(self._PREFIXES):
            return None
        prefix = next(p for p in self._PREFIXES if fragment.startswith(p))
        filename_part = fragment[len(prefix) :]
        if filename_part == "~":
            return [CompletionResult(f"{prefix}~{os.sep}")]
        dirname = os.path.dirname(filename_part)
        listing_dir = os.path.expanduser(dirname) if dirname else os.curdir
        try:
            entries = sorted(os.listdir(listing_dir))
        except OSError:
            return []
        display_dir = f"{dirname}{os.sep}" if dirname and dirname != os.sep else dirname
        results: list[CompletionResult] = []
        for entry in entries:
            display = entry + (os.sep if os.path.isdir(os.path.join(listing_dir, entry)) else "")
            results.append(
                CompletionResult(f"{prefix}{display_dir}{display}", display_text=display)
            )
        return self._filter(os.path.basename(filename_part), results)


class ChoicesCompleter(BaseCompleter):
    """Completes option values for any option with fixed ``choices`` (gap fix)."""

    def __init__(self, model: CompletionModel, response_filter: ResponseFilter) -> None:
        self._index = model
        self._filter = response_filter

    def complete(self, parsed: ParsedResult) -> list[CompletionResult] | None:
        if parsed.current_param is None or parsed.current_fragment is None:
            return None
        arg = self._index.get_argument_data(
            parsed.lineage, parsed.current_command, parsed.current_param
        )
        if arg is None or not arg.choices:
            # Fall back to the global table - a global option's value (e.g.
            # --output) is keyed under ROOT, not the current subcommand.
            arg = self._index.get_argument_data([], ROOT, parsed.current_param)
        if arg is None or not arg.choices:
            return None
        return self._filter(parsed.current_fragment, [CompletionResult(c) for c in arg.choices])
