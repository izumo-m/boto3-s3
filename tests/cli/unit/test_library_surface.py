"""The CLI consumes the library only through its public contract.

Everything ``boto3_s3_cli`` imports from ``boto3_s3`` must be public: a name
imported from the package root must be in ``boto3_s3.__all__`` (or be a
submodule), and a name imported from a submodule must be in that module's
``__all__`` - the documented building-block surfaces (docs/cli.md section 3).
Attribute access through a module alias (``globsieve.compile``,
``crtsupport.should_use_crt``) is held to the same bar. A library-side rename
or privatization then fails here, at the boundary, instead of at a user's
runtime - and a CLI-side reach into a private helper fails the moment it is
written.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
from pathlib import Path

import boto3_s3
import boto3_s3_cli

_CLI_ROOT = Path(boto3_s3_cli.__file__).parent


def _is_submodule(name: str) -> bool:
    return importlib.util.find_spec(f"boto3_s3.{name}") is not None


def _module_all(module_name: str) -> frozenset[str]:
    return frozenset(importlib.import_module(module_name).__all__)


def _parsed_sources() -> list[tuple[Path, ast.Module]]:
    return [(path, ast.parse(path.read_text())) for path in sorted(_CLI_ROOT.rglob("*.py"))]


def _collect_violations() -> tuple[list[str], list[str], list[str]]:
    """Walk the CLI sources; return (root, submodule, attribute) violations."""
    root_bad: list[str] = []
    sub_bad: list[str] = []
    attr_bad: list[str] = []
    root_all = frozenset(boto3_s3.__all__)
    for path, tree in _parsed_sources():
        rel = path.relative_to(_CLI_ROOT)
        # Names this file binds to boto3_s3 submodules (for the attribute pass).
        module_aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module == "boto3_s3":
                    for alias in node.names:
                        if _is_submodule(alias.name):
                            module_aliases[alias.asname or alias.name] = f"boto3_s3.{alias.name}"
                        elif alias.name not in root_all:
                            root_bad.append(f"{rel}: from boto3_s3 import {alias.name}")
                elif node.module.startswith("boto3_s3."):
                    exported = _module_all(node.module)
                    for alias in node.names:
                        if alias.name not in exported:
                            sub_bad.append(f"{rel}: from {node.module} import {alias.name}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("boto3_s3.") and alias.asname:
                        module_aliases[alias.asname] = alias.name
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in module_aliases
            ):
                module_name = module_aliases[node.value.id]
                if node.attr not in _module_all(module_name):
                    attr_bad.append(f"{rel}: {node.value.id}.{node.attr} ({module_name})")
    return root_bad, sub_bad, attr_bad


class TestCliConsumesOnlyThePublicSurface:
    def test_root_imports_are_in_the_root_all(self) -> None:
        root_bad, _, _ = _collect_violations()
        assert not root_bad, root_bad

    def test_submodule_imports_are_in_that_modules_all(self) -> None:
        _, sub_bad, _ = _collect_violations()
        assert not sub_bad, sub_bad

    def test_module_alias_attribute_access_is_public(self) -> None:
        _, _, attr_bad = _collect_violations()
        assert not attr_bad, attr_bad

    def test_the_walker_actually_sees_the_known_consumers(self) -> None:
        # Guard the guard: the walker must be looking at real sources - pin a
        # few imports that are known to exist so an empty walk cannot pass.
        seen_modules: set[str] = set()
        for _path, tree in _parsed_sources():
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    if node.module == "boto3_s3" or node.module.startswith("boto3_s3."):
                        seen_modules.add(node.module)
        assert "boto3_s3" in seen_modules
        assert "boto3_s3.fileformat" in seen_modules  # cp.py: plan_transfer/item_paths
        assert "boto3_s3.globsieve" in seen_modules  # filters.py: Matcher/PatternKind
        assert "boto3_s3.transfer" in seen_modules  # transferargs.py: the floor probe
