"""Auto-prompt (``--cli-auto-prompt``) support: an interactive prompt with
``aws s3``-faithful completion, active only when ``prompt_toolkit`` is installed.

The dispatcher touches only :mod:`resolve` (the SDK-free, prompt_toolkit-free
mode resolution) before parsing; everything else is imported lazily - only once
``--cli-auto-prompt`` actually fires on an install that has the ``autoprompt``
extra (``prompt_toolkit``). The ``--help`` / ``--version`` / usage /
normal-dispatch paths stay SDK-free (import contract, ``docs/imports.md``).

The completion engine (:mod:`model`, :mod:`parser`, :mod:`completers`) is a
port of aws-cli's ``awscli/autocomplete/`` scoped to the ``boto3-s3`` command
surface, and is pure Python (no ``prompt_toolkit``). Only :mod:`prompt` binds
``prompt_toolkit``. Design: ``docs/autoprompt.md``.
"""

from __future__ import annotations

from boto3_s3_cli.autoprompt.prompter import AutoPrompter

__all__ = ["AutoPrompter"]
