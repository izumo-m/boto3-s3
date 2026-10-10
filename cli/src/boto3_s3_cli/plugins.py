"""aws-cli's ``[plugins]`` loader, as far as it can run outside aws-cli.

aws's ``create_clidriver`` hands the config file's ``[plugins]`` section to
its ``load_plugins`` right after building its session - after the preliminary
``--profile`` / ``--debug`` scan and the config-file read, ahead of everything
else, ``--version`` and the help token included (measured). Without a
``cli_legacy_plugin_path`` entry nothing is imported. With one, that value is
split on ``os.pathsep`` onto ``sys.path``, every other entry is imported, and
then each imported module's ``awscli_initialize`` is called with aws's event
emitter. Any exception on that way - a module that does not import, one
without ``awscli_initialize``, an initializer that raises - ends the run
through aws's general handler at rc 255 with ``str()`` of the exception
(``No module named 'x'``, ``module 'x' has no attribute 'awscli_initialize'``,
measured). `load` repeats exactly those steps, on the same values (a value the
INI parse turned into an indented block fails the same Python operation).

What a plugin registers is replayed onto every botocore session this CLI
opens (`attach`), right after the session is built - where aws's own session
has them, after botocore's built-in handlers - so a handler on a botocore
event (``before-call.s3.PutObject``, ``after-call``, ``needs-retry`` ...)
runs as it does under aws. aws-cli's own events (``building-command-table``,
``top-level-args-parsed``, ``session-initialized`` ...) are never emitted
here, and a plugin that imports aws-cli's modules fails to import: those
parts of a plugin are written against aws-cli's internals, which this CLI
does not have (docs/cli/aws-differences.md).

The emitter handed to ``awscli_initialize`` only records: building a
botocore emitter would import the SDK on the informational exits, which
must stay SDK-free (design/imports.md).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import Any

from boto3_s3_cli.configfiles import ConfigValue

# The `[plugins]` key naming the directories to import plugins from (aws's
# CLI_LEGACY_PLUGIN_PATH); without it aws imports no plugin at all.
CLI_LEGACY_PLUGIN_PATH = "cli_legacy_plugin_path"


class PluginEmitter:
    """The event emitter a plugin's ``awscli_initialize`` registers on.

    It records each ``register`` / ``register_first`` / ``register_last`` /
    ``unregister`` call, in order, for `attach` to replay onto a botocore
    session's emitter. Nothing is listening yet while plugins initialize -
    aws-cli's own handlers do not exist here - so ``emit`` answers with no
    responses and ``emit_until_response`` with none.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def register(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("register", args, kwargs))

    def register_first(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("register_first", args, kwargs))

    def register_last(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("register_last", args, kwargs))

    def unregister(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("unregister", args, kwargs))

    def emit(self, *_args: Any, **_kwargs: Any) -> list[tuple[Any, Any]]:
        return []

    def emit_until_response(self, *_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        return None, None


# The registrations this run's plugins made (`load`), replayed by `attach`.
_emitter: PluginEmitter | None = None


def load(section: Mapping[str, ConfigValue]) -> None:
    """Import and initialize the plugins ``section`` names, aws's way.

    Raises whatever the import or an initializer raises; the caller reports it
    as aws's general handler does.
    """
    global _emitter
    mapping: dict[str, Any] = dict(section)
    plugin_path: Any = mapping.pop(CLI_LEGACY_PLUGIN_PATH, None)
    if plugin_path is None:
        return
    for dirname in plugin_path.split(os.pathsep):
        sys.path.append(dirname)
    modules = [_import_plugin(path) for path in mapping.values()]
    emitter = PluginEmitter()
    _emitter = emitter
    for module in modules:
        module.awscli_initialize(emitter)


def _import_plugin(path: Any) -> Any:
    """aws's ``_import_plugins`` for one entry: the module the path names."""
    if "." not in path:
        return __import__(path)
    module = path.rsplit(".", 1)[1]
    return __import__(path, fromlist=[module])


def attach(event_emitter: Any) -> None:
    """Replay the plugins' registrations onto a botocore session's emitter."""
    if _emitter is None:
        return
    for method, args, kwargs in _emitter.calls:
        getattr(event_emitter, method)(*args, **kwargs)


__all__ = ["CLI_LEGACY_PLUGIN_PATH", "PluginEmitter", "attach", "load"]
