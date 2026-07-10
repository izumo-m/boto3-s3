"""Auto-prompt (``--cli-auto-prompt``) support: an interactive prompt with
``aws s3``-faithful completion, active only when ``prompt_toolkit`` is installed.

The dispatcher touches only ``resolve`` (the SDK-free, prompt_toolkit-free
mode resolution) before parsing; everything else is imported lazily - only once
``--cli-auto-prompt`` actually fires on an install that has the ``autoprompt``
extra (``prompt_toolkit``). The ``--help`` / ``--version`` / usage /
normal-dispatch paths stay SDK-free (import contract, ``docs/imports.md``).

The completion engine (``model``, ``parser``, ``completers``) is a
port of aws-cli's ``awscli/autocomplete/`` scoped to the ``boto3-s3`` command
surface, and is pure Python (no ``prompt_toolkit``). Only ``prompt`` binds
``prompt_toolkit``. Design: ``docs/autoprompt.md``.
"""
