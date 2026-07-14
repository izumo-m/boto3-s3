"""Comparison tables and regression flags over stored benchmark records.

The E2E headline is the startup-adjusted ratio: ``net = median(work) -
median(startup_minimal)`` per side, ``ratio = net_ours / net_aws``. The
subtraction is on by default because aws-cli v2's frozen-binary startup is
hundreds of milliseconds - a raw wall-clock ratio would understate a real
regression on short scenarios. Raw medians stay in the table for
transparency, and the startup probes themselves are reported (and baseline-
flagged) on raw medians, since startup growth is its own regression class.

Flag rules (threshold defaults to 1.10):
- E2E work scenario with a baseline: ``(ratio now) / (ratio baseline)`` over
  the threshold - the aws side is the same-run control, so this survives
  host noise between runs. Without a baseline: the ratio itself.
- Startup probes and in-process scenarios: median now / median baseline over
  the threshold (in-process runs are network-free and deterministic enough
  to compare across runs directly).
"""

from __future__ import annotations

import statistics
from typing import TYPE_CHECKING

from benchmarks.core import Side

if TYPE_CHECKING:
    from collections.abc import Sequence

STARTUP_PROBES = ("startup_version", "startup_minimal")

_Record = dict[str, object]
_Key = tuple[str, str]


def _index(records: Sequence[_Record]) -> dict[_Key, _Record]:
    return {(str(r["scenario"]), str(r["engine"])): r for r in records}


def _comparable(record: _Record, base_record: _Record | None) -> _Record | None:
    """Reject a baseline row whose workload differs (e.g. a --quick baseline).

    Scenario names stay the same across scales, so the dimensions (file
    counts, sizes) are what actually guarantee like-for-like timing.
    """
    if base_record is None or record.get("dimensions") != base_record.get("dimensions"):
        return None
    return base_record


def _samples(record: _Record, side: Side) -> list[float] | None:
    samples = record.get("samples")
    if not isinstance(samples, dict):
        return None
    values = samples.get(side.value)
    if not values:
        return None
    return [float(v) for v in values]


def _median(record: _Record | None, side: Side) -> float | None:
    if record is None:
        return None
    values = _samples(record, side)
    return statistics.median(values) if values else None


def _spread(record: _Record, side: Side) -> float | None:
    values = _samples(record, side)
    if not values:
        return None
    return (max(values) - min(values)) / 2.0


def _net(
    raw: float | None, index: dict[_Key, _Record], engine: str, side: Side, adjust: bool
) -> float | None:
    """The startup-adjusted median, or None when it cannot be computed.

    A non-positive net (work faster than the startup probe) means the
    scenario is too small to separate from startup on this host; it renders
    as ``-`` rather than producing a nonsense ratio.
    """
    if raw is None:
        return None
    if not adjust:
        return raw
    startup = _median(index.get(("startup_minimal", engine)), side)
    if startup is None:
        return raw
    net = raw - startup
    return net if net > 0 else None


def _fmt_seconds(value: float | None) -> str:
    return f"{value:.3f}s" if value is not None else "-"


def _fmt_ratio(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "-"


def _fmt_delta(value: float | None) -> str:
    return f"{value:+.1%}" if value is not None else "-"


def _table(header: list[str], rows: list[list[str]]) -> str:
    widths = [len(cell) for cell in header]
    for row in rows:
        widths = [max(w, len(cell)) for w, cell in zip(widths, row, strict=True)]
    lines = [
        "  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip()
        for row in [header, *rows]
    ]
    return "\n".join(lines)


def _describe(meta: _Record) -> str:
    dirty = "-dirty" if meta.get("git_dirty") else ""
    return f"{meta.get('git_rev')}{dirty} @ {meta.get('timestamp_utc')}"


def render(
    current: tuple[_Record, list[_Record]],
    baseline: tuple[_Record, list[_Record]] | None,
    *,
    adjust: bool = True,
    threshold: float = 1.10,
) -> tuple[str, bool]:
    """Render one run's table (optionally against a baseline run).

    Returns the text and whether any regression flag fired.
    """
    meta, records = current
    mode = str(meta.get("mode"))
    base_index: dict[_Key, _Record] = {}
    base_meta: _Record | None = None
    if baseline is not None:
        base_meta, base_records = baseline
        base_index = _index(base_records)
    index = _index(records)

    flagged = False
    rows: list[list[str]] = []
    if mode == "e2e":
        header = [
            "scenario",
            "engine",
            "ours(raw)",
            "ours(net)",
            "spread",
            "aws(raw)",
            "aws(net)",
            "ratio",
            "base-ratio",
            "dbase",
            "flag",
        ]
    else:
        header = ["scenario", "engine", "ours(med)", "spread", "base(med)", "dbase", "flag"]

    for record in records:
        scenario = str(record["scenario"])
        engine = str(record["engine"])
        base_record = _comparable(record, base_index.get((scenario, engine)))
        ours_raw = _median(record, Side.OURS)
        spread = _spread(record, Side.OURS)
        is_startup = scenario in STARTUP_PROBES

        if mode != "e2e" or is_startup:
            # Cross-run comparison on raw medians (in-process rows and the
            # startup probes, which ARE the startup cost being tracked).
            base_raw = _median(base_record, Side.OURS)
            delta = (ours_raw / base_raw - 1.0) if ours_raw and base_raw else None
            flag = delta is not None and (1.0 + delta) > threshold
            flagged = flagged or flag
            if mode == "e2e":
                aws_raw = _median(record, Side.AWS)
                rows.append(
                    [
                        scenario,
                        engine,
                        _fmt_seconds(ours_raw),
                        "-",
                        _fmt_seconds(spread),
                        _fmt_seconds(aws_raw),
                        "-",
                        "-",
                        "-",
                        _fmt_delta(delta),
                        "!" if flag else "",
                    ]
                )
            else:
                rows.append(
                    [
                        scenario,
                        engine,
                        _fmt_seconds(ours_raw),
                        _fmt_seconds(spread),
                        _fmt_seconds(base_raw),
                        _fmt_delta(delta),
                        "!" if flag else "",
                    ]
                )
            continue

        aws_raw = _median(record, Side.AWS)
        net_ours = _net(ours_raw, index, engine, Side.OURS, adjust)
        net_aws = _net(aws_raw, index, engine, Side.AWS, adjust)
        ratio = (net_ours / net_aws) if net_ours and net_aws else None
        base_ratio: float | None = None
        if base_record is not None:
            base_net_ours = _net(
                _median(base_record, Side.OURS), base_index, engine, Side.OURS, adjust
            )
            base_net_aws = _net(
                _median(base_record, Side.AWS), base_index, engine, Side.AWS, adjust
            )
            if base_net_ours and base_net_aws:
                base_ratio = base_net_ours / base_net_aws
        if ratio is not None and base_ratio is not None:
            delta = ratio / base_ratio - 1.0
            flag = (1.0 + delta) > threshold
        elif ratio is not None:
            delta = None
            flag = ratio > threshold
        else:
            delta = None
            flag = False
        flagged = flagged or flag
        rows.append(
            [
                scenario,
                engine,
                _fmt_seconds(ours_raw),
                _fmt_seconds(net_ours),
                _fmt_seconds(spread),
                _fmt_seconds(aws_raw),
                _fmt_seconds(net_aws),
                _fmt_ratio(ratio),
                _fmt_ratio(base_ratio),
                _fmt_delta(delta),
                "!" if flag else "",
            ]
        )

    lines = [f"== {mode} == {_describe(meta)}"]
    if base_meta is not None:
        lines.append(f"baseline: {_describe(base_meta)}")
    if mode == "e2e":
        note = (
            "net = median - startup_minimal median (per side)"
            if adjust
            else "startup adjustment disabled (--no-adjust-startup)"
        )
        lines.append(note)
    lines.append("")
    lines.append(_table(header, rows))
    return "\n".join(lines), flagged
