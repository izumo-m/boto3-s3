"""Import contract: what importing ``boto3_s3`` is allowed to load.

The public surface is re-exported lazily (PEP 562 ``__getattr__`` in
``boto3_s3/__init__.py``; policy in ``docs/imports.md``). These tests pin the
contract that motivates it:

- ``import boto3_s3`` (and pure helpers like ``globsieve``) load **no** AWS
  SDK module (docs/imports.md section 1).

Module-loading cases run in a fresh interpreter (``python -c``) so imports
already made by the test runner can't mask a regression.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_SDK_SNIFFER = """
    import sys

    def sdk_modules():
        roots = ("boto3", "botocore", "s3transfer")
        return sorted(m for m in sys.modules if m.partition(".")[0] in roots)
"""


def _run_fresh(code: str) -> None:
    """Run *code* (dedented, after the sniffer prelude) in a fresh interpreter."""
    source = textwrap.dedent(_SDK_SNIFFER) + textwrap.dedent(code)
    result = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, (
        f"\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


class TestLibraryImportContract:
    def test_bare_import_loads_no_sdk_and_no_submodule(self) -> None:
        _run_fresh(
            """
            import boto3_s3

            assert not sdk_modules(), sdk_modules()
            lazy = [m for m in sys.modules if m.startswith("boto3_s3.")]
            assert not lazy, lazy
            """
        )

    def test_globsieve_alone_stays_pure(self) -> None:
        # The pure-Python pattern engine must be usable without the SDK tax.
        _run_fresh(
            """
            import boto3_s3.globsieve

            assert not sdk_modules(), sdk_modules()
            """
        )

    def test_types_alone_stays_pure(self) -> None:
        # docs/imports.md section 1 item 2 names types among the pure modules: the
        # record/enum definitions must not drag in the SDK on import.
        _run_fresh(
            """
            import boto3_s3.types

            assert not sdk_modules(), sdk_modules()
            """
        )

    def test_exceptions_alone_stays_pure(self) -> None:
        # docs/imports.md section 1 item 2 names exceptions among the pure modules: the
        # taxonomy is plain Python and must stay SDK-free on import.
        _run_fresh(
            """
            import boto3_s3.exceptions

            assert not sdk_modules(), sdk_modules()
            """
        )

    def test_awsconfig_alone_stays_pure(self) -> None:
        # The AWS-config-file reader is an opt-in building block; its botocore
        # touch (and the default boto3 session) are deferred into the read path,
        # so importing the module must not pull the SDK.
        _run_fresh(
            """
            import boto3_s3.awsconfig

            assert not sdk_modules(), sdk_modules()
            """
        )

    def test_etagcompare_alone_stays_pure(self) -> None:
        # The ETag content-comparison filter is an opt-in building block; its one
        # SDK touch (s3transfer's ChunksizeAdjuster) is deferred into the compute
        # path, so importing the module must not pull the SDK.
        _run_fresh(
            """
            import boto3_s3.etagcompare

            assert not sdk_modules(), sdk_modules()
            """
        )

    def test_checksumcompare_alone_stays_pure(self) -> None:
        # The native-checksum filter is an opt-in building block; its SDK touches
        # (the boto3 client via s3.resolve, botocore's ClientError, and the
        # optional awscrt fast checksums) are deferred into the construct / compute
        # paths, so importing the module must not pull the SDK.
        _run_fresh(
            """
            import boto3_s3.checksumcompare

            assert not sdk_modules(), sdk_modules()
            """
        )

    def test_request_params_stay_pure(self) -> None:
        # The cp param rules are pure string logic: the CLI imports them on
        # its run path and the library's tests exercise them SDK-free.
        _run_fresh(
            """
            import boto3_s3.requestparams

            assert not sdk_modules(), sdk_modules()
            """
        )

    def test_version_resolves_without_the_sdk(self) -> None:
        _run_fresh(
            """
            import boto3_s3

            assert boto3_s3.__version__
            assert not sdk_modules(), sdk_modules()
            """
        )


class TestLazyExports:
    def test_every_public_symbol_resolves(self) -> None:
        # Guards __all__ / _EXPORT_HOMES drift in both directions: every
        # __all__ name must resolve, and the resolution map (plus the
        # special-cased __version__) must carry exactly __all__ - a stale
        # entry on either side fails here. The TYPE_CHECKING leg of the
        # three-way agreement is pinned by the source-level test below.
        import boto3_s3

        for name in boto3_s3.__all__:
            assert getattr(boto3_s3, name) is not None, name
        assert set(boto3_s3._EXPORT_HOMES) | {"__version__"} == set(boto3_s3.__all__)

    def test_type_checking_imports_mirror_the_export_map(self) -> None:
        # getattr resolution runs through _EXPORT_HOMES and never sees the
        # TYPE_CHECKING block, so the three-way agreement promised in
        # docs/imports.md section 3 needs a source-level check: the block
        # must import exactly the _EXPORT_HOMES names, each from its home
        # module, and annotate the special-cased __version__.
        import boto3_s3

        tree = ast.parse(Path(boto3_s3.__file__).read_text(encoding="utf-8"))
        blocks = [
            node.body
            for node in tree.body
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "TYPE_CHECKING"
        ]
        assert len(blocks) == 1, "expected exactly one TYPE_CHECKING block"
        imported: dict[str, str] = {}
        annotated: list[str] = []
        for stmt in blocks[0]:
            if isinstance(stmt, ast.ImportFrom):
                assert stmt.module is not None
                for alias in stmt.names:
                    imported[alias.asname or alias.name] = stmt.module
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                annotated.append(stmt.target.id)
        assert imported == boto3_s3._EXPORT_HOMES
        assert annotated == ["__version__"]

    def test_every_root_export_is_in_its_home_module_all(self) -> None:
        # The two-tier surface contract (docs/imports.md): a root export must
        # also appear in its home module's __all__, so `from boto3_s3.types
        # import *` and the root agree on what is public. getattr-based
        # resolution above cannot catch a home-__all__ omission.
        import importlib

        import boto3_s3

        for name, home in boto3_s3._EXPORT_HOMES.items():
            module = importlib.import_module(home)
            assert name in module.__all__, f"{name} missing from {home}.__all__"

    def test_resolved_symbols_are_the_real_ones(self) -> None:
        import boto3_s3
        from boto3_s3.s3 import S3

        assert boto3_s3.S3 is S3

    def test_dir_lists_the_public_surface(self) -> None:
        import boto3_s3

        assert set(boto3_s3.__all__) <= set(dir(boto3_s3))

    def test_unknown_attribute_raises_attribute_error(self) -> None:
        import boto3_s3

        with pytest.raises(AttributeError, match="no_such_symbol"):
            _ = boto3_s3.no_such_symbol  # pyright: ignore[reportAttributeAccessIssue]
