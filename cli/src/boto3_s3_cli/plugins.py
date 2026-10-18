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
through aws's entry-point handlers (`cli.main` reports it). `load` repeats
exactly those steps, on the same values (a value the INI parse turned into an
indented block fails the same Python operation).

The emitter a plugin initializes on is a port of botocore's
``HierarchicalEmitter``, which the one aws hands it extends (its
``LazyInitEmitter``, with no ``EventAliaser`` around it as stock botocore
puts around a session's emitter) - so registering, unregistering and
emitting behave, and fail, as they do there while the plugins load: an event
name that is not a string, a handler that is not callable or takes no
``**kwargs``, a ``unique_id`` registered with and without its counter in turn,
an unregister of a name the tree does not hold. It is SDK-free because it
runs on the informational exits, which must not load the SDK
(design/imports.md). What it cannot hold is what aws's emitter already holds
at that point - botocore's and aws-cli's own handlers - so an event a plugin
emits while it initializes reaches only plugin handlers, and an unregister
aimed at one of those built-ins takes effect when the first session replays
it rather than while the plugins load. The same goes for an unregister that
ends on a name holding no handlers here: on aws's tree the built-ins usually
give that name some, and botocore's failure on a name that has none
(``'NoneType' object has no attribute 'first'``) is then raised by the
replay, when the run first builds a client.

Every call that changes that emitter is recorded and replayed onto each
botocore session this CLI opens (`attach`), right after the session is built
- where aws's own session has them, after botocore's built-in handlers - so a
handler on a botocore event (``before-call.s3.PutObject``, ``after-call``,
``needs-retry`` ...) runs as it does under aws. A call aws's emitter answers
without touching its handlers (a ``unique_id`` registered again or counted up,
one unregistered that was never registered or counted down) is not replayed:
stock botocore's aliaser would read the event name of such a call, which aws's
emitter never does. aws-cli's own events (``building-command-table``,
``top-level-args-parsed``, ``session-initialized`` ...) are never emitted
here, and a plugin that imports aws-cli's modules fails to import: those
parts of a plugin are written against aws-cli's internals, which this CLI
does not have (docs/cli/aws-differences.md).
"""

from __future__ import annotations

import copy
import inspect
import os
import sys
from collections import deque
from collections.abc import Mapping
from typing import Any, NamedTuple, cast

from boto3_s3_cli.configfiles import ConfigValue

# The `[plugins]` key naming the directories to import plugins from (aws's
# CLI_LEGACY_PLUGIN_PATH); without it aws imports no plugin at all.
CLI_LEGACY_PLUGIN_PATH = "cli_legacy_plugin_path"

# The three sections of a name's handlers, in calling order (botocore's
# _FIRST / _MIDDLE / _LAST).
_FIRST = 0
_MIDDLE = 1
_LAST = 2
_SECTION_METHODS = ("register_first", "register", "register_last")


class _NodeList(NamedTuple):
    first: list[Any]
    middle: list[Any]
    last: list[Any]


class _PrefixTrie:
    """botocore's ``PrefixTrie``: handlers keyed by dot-separated event names.

    A name's handlers also answer every longer name under it (``before-call``
    answers ``before-call.s3.PutObject``), most specific first, and a ``*``
    part matches any one part. A node a longer name created on the way holds
    no handler list at all (``values`` is None), which is what an unregister
    that stops on it trips over.
    """

    def __init__(self) -> None:
        self._root: dict[str, Any] = {"chunk": None, "children": {}, "values": None}

    def append_item(self, key: Any, value: Any, section: int = _MIDDLE) -> None:
        key_parts = key.split(".")
        current: dict[str, Any] = self._root
        for part in key_parts:
            if part not in current["children"]:
                new_child: dict[str, Any] = {"chunk": part, "values": None, "children": {}}
                current["children"][part] = new_child
                current = new_child
            else:
                current = current["children"][part]
        if current["values"] is None:
            current["values"] = _NodeList([], [], [])
        current["values"][section].append(value)

    def prefix_search(self, key: Any) -> deque[Any]:
        collected: deque[Any] = deque()
        key_parts = key.split(".")
        stack = [(self._root, 0)]
        while stack:
            current_node, index = stack.pop()
            if current_node["values"]:
                node_list = current_node["values"]
                complete_order = node_list.first + node_list.middle + node_list.last
                collected.extendleft(reversed(complete_order))
            if index != len(key_parts):
                children = current_node["children"]
                directs = children.get(key_parts[index])
                wildcard = children.get("*")
                if wildcard is not None:
                    stack.append((wildcard, index + 1))
                if directs is not None:
                    stack.append((directs, index + 1))
        return collected

    def __copy__(self) -> _PrefixTrie:
        new_copy = self.__class__()
        new_copy._root = self._copy_node(self._root)
        return new_copy

    def _copy_node(self, node: dict[str, Any]) -> dict[str, Any]:
        """botocore's ``_recursive_copy``: the structure, not the handlers."""
        copied: dict[str, Any] = {}
        for key, value in node.items():
            if isinstance(value, _NodeList):
                copied[key] = _NodeList(list(value.first), list(value.middle), list(value.last))
            elif isinstance(value, dict):
                copied[key] = self._copy_node(cast("dict[str, Any]", value))
            else:
                copied[key] = value
        return copied

    def remove_item(self, key: Any, value: Any) -> None:
        self._remove_item(self._root, key.split("."), value, 0)

    def _remove_item(
        self, current_node: dict[str, Any], key_parts: list[str], value: Any, index: int
    ) -> None:
        if index < len(key_parts):
            next_node = current_node["children"].get(key_parts[index])
            if next_node is None:
                raise ValueError("key is not in trie: {}".format(".".join(key_parts)))
            self._remove_item(next_node, key_parts, value, index + 1)
            if index == len(key_parts) - 1:
                node_list = next_node["values"]
                if node_list is None:
                    # botocore fails here (`None.first`): the name only leads
                    # to longer ones. This tree lacks the SDK's built-ins,
                    # which give most event names handlers of their own
                    # (`before-call.s3`), so the call is left to the replay,
                    # whose tree has them.
                    return
                if value in node_list.first:
                    node_list.first.remove(value)
                elif value in node_list.middle:
                    node_list.middle.remove(value)
                elif value in node_list.last:
                    node_list.last.remove(value)
            if not next_node["children"] and not next_node["values"]:
                del current_node["children"][key_parts[index]]


class BaseEventHooks:
    """botocore's ``BaseEventHooks``: the registration checks.

    The class names are botocore's own, because Python names them in the
    report of a call with the wrong arguments (``BaseEventHooks.register()
    got an unexpected keyword argument ...``), which ends the run at load.
    """

    def emit(self, event_name: Any, **kwargs: Any) -> list[tuple[Any, Any]]:
        return []

    def register(
        self,
        event_name: Any,
        handler: Any,
        unique_id: Any = None,
        unique_id_uses_count: bool = False,
    ) -> None:
        self._verify_and_register(event_name, handler, unique_id, _MIDDLE, unique_id_uses_count)

    def register_first(
        self,
        event_name: Any,
        handler: Any,
        unique_id: Any = None,
        unique_id_uses_count: bool = False,
    ) -> None:
        self._verify_and_register(event_name, handler, unique_id, _FIRST, unique_id_uses_count)

    def register_last(
        self,
        event_name: Any,
        handler: Any,
        unique_id: Any = None,
        unique_id_uses_count: bool = False,
    ) -> None:
        self._verify_and_register(event_name, handler, unique_id, _LAST, unique_id_uses_count)

    def _verify_and_register(
        self,
        event_name: Any,
        handler: Any,
        unique_id: Any,
        section: int,
        unique_id_uses_count: bool,
    ) -> None:
        self._verify_is_callable(handler)
        self._verify_accept_kwargs(handler)
        self._register_section(event_name, handler, unique_id, unique_id_uses_count, section)

    def _verify_is_callable(self, func: Any) -> None:
        if not callable(func):
            raise ValueError(f"Event handler {func} must be callable.")

    def _verify_accept_kwargs(self, func: Any) -> bool | None:
        try:
            if not inspect.getfullargspec(func)[2]:
                raise ValueError(f"Event handler {func} must accept keyword arguments (**kwargs)")
        except TypeError:
            # A callable that cannot be inspected passes - which callables
            # those are is the interpreter's (docs/cli/aws-differences.md).
            return False
        return None

    def _register_section(
        self,
        event_name: Any,
        handler: Any,
        unique_id: Any,
        unique_id_uses_count: bool,
        section: int,
    ) -> None:
        raise NotImplementedError

    def unregister(
        self,
        event_name: Any,
        handler: Any = None,
        unique_id: Any = None,
        unique_id_uses_count: bool = False,
    ) -> None:
        pass


class HierarchicalEmitter(BaseEventHooks):
    """botocore's ``HierarchicalEmitter``, recording what it does for `attach`.

    ``calls`` holds every register / unregister that changed the emitter, as
    ``(method, args)`` in the order they were made.
    """

    def __init__(self) -> None:
        self._lookup_cache: dict[Any, deque[Any]] = {}
        self._handlers = _PrefixTrie()
        self._unique_id_handlers: dict[Any, dict[str, Any]] = {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def __copy__(self) -> HierarchicalEmitter:
        """botocore's copy: a detached emitter, so it records for itself.

        What a plugin registers on a copy reaches no session on aws, and is
        not replayed here (`attach` replays the emitter `load` made).
        """
        new_instance = self.__class__()
        new_state = self.__dict__.copy()
        new_state["_handlers"] = copy.copy(self._handlers)
        new_state["_unique_id_handlers"] = copy.copy(self._unique_id_handlers)
        new_state["calls"] = list(self.calls)
        new_instance.__dict__ = new_state
        return new_instance

    def _emit(
        self, event_name: Any, kwargs: dict[str, Any], stop_on_response: bool = False
    ) -> list[tuple[Any, Any]]:
        handlers_to_call = self._lookup_cache.get(event_name)
        if handlers_to_call is None:
            handlers_to_call = self._handlers.prefix_search(event_name)
            self._lookup_cache[event_name] = handlers_to_call
        elif not handlers_to_call:
            return []
        kwargs["event_name"] = event_name
        responses: list[tuple[Any, Any]] = []
        for handler in handlers_to_call:
            response = handler(**kwargs)
            responses.append((handler, response))
            if stop_on_response and response is not None:
                return responses
        return responses

    def emit(self, event_name: Any, **kwargs: Any) -> list[tuple[Any, Any]]:
        return self._emit(event_name, kwargs)

    def emit_until_response(self, event_name: Any, **kwargs: Any) -> tuple[Any, Any]:
        responses = self._emit(event_name, kwargs, stop_on_response=True)
        if responses:
            return responses[-1]
        return (None, None)

    def _register_section(
        self,
        event_name: Any,
        handler: Any,
        unique_id: Any,
        unique_id_uses_count: bool,
        section: int,
    ) -> None:
        if unique_id is not None:
            if unique_id in self._unique_id_handlers:
                count = self._unique_id_handlers[unique_id].get("count", None)
                if unique_id_uses_count:
                    if not count:
                        raise ValueError(
                            f"Initial registration of  unique id {unique_id} was "
                            "specified to use a counter. Subsequent register "
                            "calls to unique id must specify use of a counter "
                            "as well."
                        )
                    self._unique_id_handlers[unique_id]["count"] += 1
                elif count:
                    raise ValueError(
                        f"Initial registration of unique id {unique_id} was "
                        "specified to not use a counter. Subsequent "
                        "register calls to unique id must specify not to "
                        "use a counter as well."
                    )
                return
            self._handlers.append_item(event_name, handler, section=section)
            unique_id_handler_item: dict[str, Any] = {"handler": handler}
            if unique_id_uses_count:
                unique_id_handler_item["count"] = 1
            self._unique_id_handlers[unique_id] = unique_id_handler_item
        else:
            self._handlers.append_item(event_name, handler, section=section)
        self._lookup_cache = {}
        self.calls.append(
            (_SECTION_METHODS[section], (event_name, handler, unique_id, unique_id_uses_count))
        )

    def unregister(
        self,
        event_name: Any,
        handler: Any = None,
        unique_id: Any = None,
        unique_id_uses_count: bool = False,
    ) -> None:
        call = ("unregister", (event_name, handler, unique_id, unique_id_uses_count))
        if unique_id is not None:
            try:
                count = self._unique_id_handlers[unique_id].get("count", None)
            except KeyError:
                return
            if unique_id_uses_count:
                if count is None:
                    raise ValueError(
                        f"Initial registration of unique id {unique_id} was specified to "
                        "use a counter. Subsequent unregister calls to unique "
                        "id must specify use of a counter as well."
                    )
                if count == 1:
                    handler = self._unique_id_handlers.pop(unique_id)["handler"]
                else:
                    self._unique_id_handlers[unique_id]["count"] -= 1
                    return
            else:
                if count:
                    raise ValueError(
                        f"Initial registration of unique id {unique_id} was specified "
                        "to not use a counter. Subsequent unregister calls "
                        "to unique id must specify not to use a counter as "
                        "well."
                    )
                handler = self._unique_id_handlers.pop(unique_id)["handler"]
        # Recorded even when nothing here matches: the handler it names may be
        # one of the session's built-ins, which only the replay can reach.
        self.calls.append(call)
        try:
            self._handlers.remove_item(event_name, handler)
            self._lookup_cache = {}
        except ValueError:
            pass


class PluginHandlerError(Exception):
    """What a plugin handler raised that is not an ordinary failure.

    aws's entry point and transfer paths treat an `AssertionError` from a
    handler like any other `Exception`, while this CLI reserves a bare one for
    its own invariants and lets it surface - and a `BaseException` outside
    the `Exception` family (``GeneratorExit``, a plugin's own subclass) would
    escape every handler as a traceback. A replayed handler re-raises either as
    this, with the same message, so it is reported as aws reports an
    `Exception` there. `KeyboardInterrupt` and `SystemExit` pass unchanged.
    """


class _ReplayedHandler:
    """A plugin's handler as registered on a botocore session (see `attach`)."""

    def __init__(self, handler: Any) -> None:
        self.handler = handler

    def __call__(self, **kwargs: Any) -> Any:
        try:
            return self.handler(**kwargs)
        except (KeyboardInterrupt, SystemExit):
            raise
        except AssertionError as exc:
            raise PluginHandlerError(str(exc)) from exc
        except Exception:
            raise
        except BaseException as exc:
            raise PluginHandlerError(str(exc)) from exc

    def __eq__(self, other: object) -> bool:
        # Equal to the handler it wraps, so botocore's `remove_item` finds it
        # for an unregister of that handler, as aws's emitter finds the bare
        # one - in the same order as the SDK's own handlers around it.
        if isinstance(other, _ReplayedHandler):
            other = other.handler
        return bool(self.handler == other)

    def __hash__(self) -> int:
        try:
            return hash(self.handler)
        except TypeError:
            return id(self)

    def __repr__(self) -> str:
        return repr(self.handler)


# The emitter this run's plugins initialized on (`load`), replayed by `attach`.
_emitter: HierarchicalEmitter | None = None
# One wrapper per distinct handler, so an unregister replays onto the very
# object its register put on the session.
_replayed: list[_ReplayedHandler] = []


def load(section: Mapping[str, ConfigValue]) -> None:
    """Import and initialize the plugins ``section`` names, aws's way.

    Starts from nothing each time (aws builds a new emitter with each driver).
    Raises whatever the import or an initializer raises; the caller reports it
    as aws's entry point does.
    """
    global _emitter
    _emitter = None
    _replayed.clear()
    mapping: dict[str, Any] = dict(section)
    plugin_path: Any = mapping.pop(CLI_LEGACY_PLUGIN_PATH, None)
    if plugin_path is None:
        return
    for dirname in plugin_path.split(os.pathsep):
        sys.path.append(dirname)
    modules = [_import_plugin(path) for path in mapping.values()]
    emitter = HierarchicalEmitter()
    _emitter = emitter
    for module in modules:
        module.awscli_initialize(emitter)


def _import_plugin(path: Any) -> Any:
    """aws's ``_import_plugins`` for one entry: the module the path names."""
    if "." not in path:
        if not isinstance(path, str):
            # An indented block (a map) gets here. Python 3.10 words this
            # differently; aws's interpreter words it this way.
            raise TypeError("module name must be a string")
        return __import__(path)
    module = path.rsplit(".", 1)[1]
    return __import__(path, fromlist=[module])


def attach(event_emitter: Any) -> None:
    """Replay the plugins' registrations onto a botocore session's emitter."""
    if _emitter is None:
        return
    for method, (event_name, handler, unique_id, uses_count) in _emitter.calls:
        getattr(event_emitter, method)(
            event_name, _replayed_handler(handler, method), unique_id, uses_count
        )


def _replayed_handler(handler: Any, method: str) -> Any:
    """The object to hand botocore for ``handler`` in a replayed ``method``.

    A registration puts a wrapper on the session, one per distinct handler.
    An unregister passes the handler as it is: botocore removes the first
    entry equal to it at that name, which is the SDK's own handler when one
    is there (``validate_bucket_name``, say, even if the plugin registered
    it elsewhere too) and otherwise the wrapper, which equals what it wraps.
    """
    if handler is None or method == "unregister":
        return handler
    for replayed in _replayed:
        if replayed.handler == handler:
            return replayed
    replayed = _ReplayedHandler(handler)
    _replayed.append(replayed)
    return replayed


__all__ = [
    "CLI_LEGACY_PLUGIN_PATH",
    "BaseEventHooks",
    "HierarchicalEmitter",
    "PluginHandlerError",
    "attach",
    "load",
]
