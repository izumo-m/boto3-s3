"""The shared scenario base for the per-command parity/golden tables.

Every ``tests/utils/<cmd>_scenarios.py`` table extends ``BaseScenario``
with its command-specific inputs (seeds, workdir trees, probe keys); the six
fields here and ``resolve_argv`` are the contract every suite consumes.
A suite consults only the flags it supports (e.g. the presign suites carry no
``diff_only`` scenario and do not filter on it, replaying every scenario as a
golden).

Charter note (docs/overview.md section 3): the exit code is compared for
*every* scenario, unconditionally - ``compare_stdout`` / ``diff_only`` only
relax stdout and golden handling, never the rc.

The flag fields are keyword-only (``KW_ONLY``): subclasses append their own
positional fields (``seed`` and friends) directly after ``argv``, so the
pre-existing positional call shape ``XScenario(name, argv, seed)`` holds.
"""

from __future__ import annotations

from dataclasses import KW_ONLY, dataclass

from tests.utils.harness import BUCKET_TOKEN


@dataclass(frozen=True)
class BaseScenario:
    """One CLI invocation: the argv template plus the comparison knobs."""

    name: str
    argv: tuple[str, ...]
    _: KW_ONLY
    # False => normalized stdout is not compared (rc still is - charter).
    compare_stdout: bool = True
    # True => live aws-vs-ours diff only: no golden written or checked, and no
    # functional replay. For endpoint-relative outcomes and moto fidelity gaps.
    diff_only: bool = False
    expected_stderr_tokens_ours: tuple[str, ...] = ()
    expected_stderr_tokens_aws: tuple[str, ...] = ()
    # True => stderr must be exactly empty on BOTH sides (a --quiet contract:
    # token lists cannot express "nothing at all", an empty tuple asserts
    # nothing).
    stderr_exact_empty: bool = False


def resolve_argv(scenario: BaseScenario, bucket: str) -> list[str]:
    """Materialize the argv template against a concrete bucket name."""
    return [arg.replace(BUCKET_TOKEN, bucket) for arg in scenario.argv]
