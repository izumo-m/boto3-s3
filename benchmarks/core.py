"""Mode-independent benchmark plumbing: sides, statistics, result records.

The timing loops themselves live in the mode modules (`e2e.py`,
`inprocess.py`); this module holds what both share - the A/B side model,
sample statistics, and the result shape `results.py` serializes.
"""

from __future__ import annotations

import enum
import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


class BenchmarkError(Exception):
    """A harness failure: bad environment, unexpected rc, or an unstubbed call."""


class VerificationError(BenchmarkError):
    """A scenario's warmup verification failed, so its timings cannot be trusted.

    Raised before any timed sample is recorded: a run that silently did no
    work (wrong prefix, empty tree) would otherwise produce fake-fast numbers.
    """


class Side(enum.Enum):
    """Which CLI produced a sample. The values are the JSONL sample keys."""

    OURS = "boto3-s3"
    AWS = "aws"


@dataclass(frozen=True)
class Stats:
    """Summary of one sample list; the median is the headline number."""

    median: float
    minimum: float
    maximum: float
    n: int

    @property
    def spread(self) -> float:
        """Half the min-max range - the ``±`` printed next to the median."""
        return (self.maximum - self.minimum) / 2.0


def summarize(samples: Sequence[float]) -> Stats:
    if not samples:
        raise BenchmarkError("cannot summarize an empty sample list")
    return Stats(
        median=statistics.median(samples),
        minimum=min(samples),
        maximum=max(samples),
        n=len(samples),
    )


def round_order(round_index: int) -> tuple[Side, Side]:
    """The side execution order for one A/B round, alternating per round.

    Back-to-back runs with alternating order make linear host drift (thermal
    ramp-up, background load creeping in) contribute symmetrically to both
    sides, so it cancels in the median ratio instead of biasing one side.
    """
    if round_index % 2 == 0:
        return (Side.OURS, Side.AWS)
    return (Side.AWS, Side.OURS)


@dataclass
class ScenarioResult:
    """All samples one scenario produced in one run.

    `samples` is keyed by `Side.value`; in-process results carry only the
    `boto3-s3` key. `order` records the per-invocation execution order of the
    timed rounds (side values, in sequence) so the A/B interleaving is
    auditable from the results file.
    """

    scenario: str
    mode: str
    engine: str
    dimensions: dict[str, str]
    samples: dict[str, list[float]]
    order: list[str]

    def record(self) -> dict[str, object]:
        """The JSONL representation (one line in a results file)."""
        return {
            "kind": "result",
            "scenario": self.scenario,
            "mode": self.mode,
            "engine": self.engine,
            "dimensions": self.dimensions,
            "samples": self.samples,
            "order": self.order,
            "unit": "s",
        }
