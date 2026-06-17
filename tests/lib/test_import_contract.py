"""Import contract: what importing ``boto3_s3`` is allowed to load.

The public surface is re-exported lazily (PEP 562 ``__getattr__`` in
``boto3_s3/__init__.py``; policy in ``docs/imports.md``). These tests pin the
contract that motivates it:

- ``import boto3_s3`` (and pure helpers like ``globsieve``) load **no** AWS
  SDK module - ``boto3`` alone drags in ``s3transfer`` via its ``compat``
  module, so an eager re-export taxes every importer ~100ms.
- Reaching the ``S3`` entry point may load ``botocore.exceptions`` (error
  translation) but still neither ``boto3``, ``s3transfer``, nor botocore's
  client machinery; those wait until a default client is actually built.

Module-loading cases run in a fresh interpreter (``python -c``) so imports
already made by the test runner can't mask a regression.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

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

    def test_etagfilter_alone_stays_pure(self) -> None:
        # The ETag content-comparison filter is an opt-in building block; its one
        # SDK touch (s3transfer's ChunksizeAdjuster) is deferred into the compute
        # path, so importing the module must not pull the SDK.
        _run_fresh(
            """
            import boto3_s3.etagfilter

            assert not sdk_modules(), sdk_modules()
            """
        )

    def test_transfer_shape_helpers_stay_pure(self) -> None:
        # The cp path/param rules are pure string logic: the CLI imports them
        # on its run path and the library's tests exercise them SDK-free.
        _run_fresh(
            """
            import boto3_s3.naming
            import boto3_s3.requestparams

            assert not sdk_modules(), sdk_modules()
            """
        )

    def test_s3_entry_point_defers_the_sdk(self) -> None:
        # botocore.exceptions is the allowed exception-translation dependency
        # (with the botocore.vendored.requests exception shims it imports);
        # the SDK proper (boto3, s3transfer, botocore's client stack) is not.
        _run_fresh(
            """
            from boto3_s3 import S3, S3Storage  # noqa: F401

            def allowed(m):
                return m == "botocore" or m.startswith(("botocore.exceptions", "botocore.vendored"))

            leaked = [m for m in sdk_modules() if not allowed(m)]
            assert not leaked, leaked
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
        # Guards __all__ / _EXPORT_HOMES / TYPE_CHECKING-import drift: a name
        # added to one but not the others fails here.
        import boto3_s3

        for name in boto3_s3.__all__:
            assert getattr(boto3_s3, name) is not None, name

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
