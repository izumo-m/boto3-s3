r"""``--exclude`` / ``--include`` handling shared by rm / cp / mv / sync.

aws-cli evaluates the two options as ONE ordered rule list (its
``AppendFilter`` action appends both to a shared ``filters`` dest) with
last-match-wins semantics, so the interleaved command-line order is
significant: ``--exclude '*' --include '*.txt'`` keeps only ``.txt`` while
the reverse keeps nothing. ``AppendFilterAction`` preserves that order as
``Exclude`` / ``Include`` rules, and ``compile_filter`` hands them to the
library's ``globsieve``, which owns aws-cli's matching: each side's base, the
absolute and Windows drive forms, the Windows fold, and the single-source
forms (the operations show a filter each entry as aws-cli matches it).
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from typing import Any, ClassVar

from boto3_s3 import Exclude, GlobFilter, Include, globsieve


class AppendFilterAction(argparse.Action):
    """Append ``--exclude`` / ``--include`` to one ordered ``filters`` list.

    The aws-cli ``AppendFilter`` equivalent: both options share ``dest``
    (``filters``) so the rule order is exactly the command-line order.

    aws-cli declares both options with ``nargs=1``, so the following token is
    taken as the pattern however it looks (``--exclude '-foo*'``).
    ``aws_nargs`` marks that count - the same marker ``mb``'s ``--tags``
    carries for its pair of tokens - and the parser base class
    (``cli._ParamValidationArgumentParser``) reads it in its
    ``_match_argument`` hook. ``nargs`` itself stays unspecified: the action
    then keeps receiving a plain string, and a real ``nargs=1`` would consume
    a dash-led token only on Python 3.12+, so it is the hook rather than the
    declaration that makes the behavior aws-cli's on every supported version.
    """

    # The count aws-cli declares; not an argparse attribute.
    aws_nargs: ClassVar[int] = 1

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        # The typeshed base signature; nargs is unspecified on both options,
        # so at runtime *values* is always a single str.
        rule = Exclude if option_string == "--exclude" else Include
        items: list[Exclude | Include] = getattr(namespace, self.dest, None) or []
        items.append(rule(str(values)))
        setattr(namespace, self.dest, items)


def add_filter_arguments(parser: argparse.ArgumentParser) -> None:
    """Register the ``--exclude`` / ``--include`` pair (rm / cp / mv / sync).

    One shared ordered ``filters`` dest: the interleaved order carries
    aws-cli's last-match-wins semantics (this module's docstring).
    """
    parser.add_argument(
        "--exclude",
        action=AppendFilterAction,
        dest="filters",
        metavar="PATTERN",
        help="leave out keys matching PATTERN; repeatable, last match wins",
    )
    parser.add_argument(
        "--include",
        action=AppendFilterAction,
        dest="filters",
        metavar="PATTERN",
        help="bring back keys matching PATTERN; repeatable, last match wins",
    )


def compile_filter(rules: Sequence[Exclude | Include] | None) -> GlobFilter | None:
    """The operation's ``filter=``: the parsed rules compiled, or ``None`` when there are none."""
    return globsieve.compile(rules) if rules else None
