"""The injectable auto-prompt interface.

Kept in its own ``prompt_toolkit``-free module so ``Context``
(``boto3_s3_cli.commands.base``) can reference the type and tests can
supply a fake without the optional extra installed. The real, ``prompt_toolkit``
-backed implementation lives in ``boto3_s3_cli.autoprompt.prompt`` and is
imported only when ``--cli-auto-prompt`` actually fires.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class AutoPrompter(ABC):
    """Turns a partial argv into a completed one via an interactive prompt."""

    @abstractmethod
    def prompt_for_args(self, argv: list[str]) -> list[str]:
        """Prompt the user (seeded with *argv*) and return the edited argv.

        *argv* is the raw token list with the auto-prompt flags removed.
        The returned list is re-dispatched by ``boto3_s3_cli.cli.main``.
        """
