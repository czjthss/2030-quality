"""Repair a single numeric time series and report its IoTDB TsQuality scores."""

import argparse
import csv
from dataclasses import dataclass
from itertools import repeat
import json
from pathlib import Path

import numpy as np

from .scoring import METRICS, evaluate
from .smooth_candidate import refine_values
from .timestamp_repair import repair_timestamps
from .trend_model import reconstruct_trend
from .value_repair import repair_values


@dataclass
class RepairResult:
    timestamps: np.ndarray
    values: np.ndarray
    source_index: np.ndarray
    timestamp_modified: np.ndarray
    value_imputed: np.ndarray
    value_modified: np.ndarray
    before: dict | None
    before_error: str | None
    after: dict
    target: float
    timestamp_details: dict
    trend_details: dict | None

    @property
    def target_met(self) -> bool:
        return all(self.after[metric] > self.target for metric in METRICS)


def score(timestamps, values):
    # Preserve small intervals on timestamp axes with a large epoch offset.
    return evaluate(timestamps - timestamps[0], values)


def repair_series(timestamps, values, *, nominal_interval=None, target=0.95, trend_model=False) -> RepairResult:
    """Repair a numeric series whose integer timestamps use a consistent unit.

    The target guides candidate selection and the pass/fail report. Source indexes refer to the
    original input order, and -1 marks inserted samples. NaN and infinities
    represent missing numeric cells. The repair assumes locally predictable
    sampling and numeric behavior.
    """
    timestamps = np.asarray(timestamps)
    values = np.asarray(values, dtype=np.float64)
    if timestamps.ndim != 1 or timestamps.dtype.kind not in "iu":
        raise ValueError("timestamps must be a one-dimensional array of integers")
    if values.shape != timestamps.shape:
        raise ValueError("values must be one-dimensional and match timestamps")
    if len(timestamps) <= 10:
        raise ValueError("IoTDB TsQuality requires more than 10 input observations")
    start, end = int(timestamps.min()), int(timestamps.max())
    if start < -(2**63) or end >= 2**63 or end - start > 2**53:
        raise ValueError("timestamps or span exceed the supported exact range; use a coarser unit")
    if not 0 < target < 1:
        raise ValueError("target must be strictly between 0 and 1")
    timestamps = timestamps.astype(np.int64, copy=False)
    if np.unique(timestamps[np.isfinite(values)]).size < 2:
        raise ValueError("at least two distinct timestamps with finite values are required")

    order = np.argsort(timestamps, kind="stable")
    try:
        before = score(timestamps[order], values[order])
        before_error = None
    except ValueError as exc:
        before = None
        before_error = str(exc)

    time_result = repair_timestamps(timestamps, nominal_interval=nominal_interval)
    repaired_t = time_result.timestamps
    source_index = time_result.source_index
    observed = source_index >= 0
    if not np.array_equal(np.sort(source_index[observed]), np.arange(len(timestamps))):
        raise RuntimeError("timestamp repair did not preserve each input row exactly once")
    if np.any(np.diff(repaired_t) <= 0):
        raise RuntimeError("timestamp repair did not return strictly increasing timestamps")
    if int(repaired_t[-1]) - int(repaired_t[0]) > 2**53:
        raise ValueError("repaired timestamp span exceeds the supported exact range")
    aligned = np.full(len(repaired_t), np.nan)
    aligned[observed] = values[source_index[observed]]
    repaired_x = repair_values(
        repaired_t - repaired_t[0], aligned, target=target, segments=time_result.segments,
    )
    finite = np.isfinite(aligned)
    repaired_x = refine_values(repaired_t, repaired_x, finite, time_result.segments, target)
    trend_details = None
    if trend_model:
        repaired_x, trend_details = reconstruct_trend(repaired_t, repaired_x, finite, target)
    if repaired_x.shape != aligned.shape or not np.all(np.isfinite(repaired_x)):
        raise RuntimeError("value repair did not return a finite value for every row")

    timestamp_modified = np.zeros(len(repaired_t), dtype=bool)
    timestamp_modified[observed] = repaired_t[observed] != timestamps[source_index[observed]]
    value_imputed = ~finite
    value_modified = ~value_imputed & (repaired_x != aligned)
    return RepairResult(
        repaired_t, repaired_x, source_index, timestamp_modified, value_imputed, value_modified,
        before, before_error, score(repaired_t, repaired_x), target,
        time_result.metadata, trend_details,
    )


def audit(result: RepairResult, timestamps, values) -> dict:
    """Summarize evidence for score changes and the extent of data edits."""
    moved = result.timestamp_modified
    edited = result.value_modified
    timestamps = np.asarray(timestamps, dtype=np.int64)
    values = np.asarray(values, dtype=np.float64)
    timestamp_changes = np.abs(result.timestamps[moved] - timestamps[result.source_index[moved]])
    value_changes = np.abs(result.values[edited] - values[result.source_index[edited]])
    observed = result.source_index >= 0
    raw_t = timestamps[result.source_index[observed]]
    raw_order = np.argsort(raw_t, kind="stable")
    original_grid = score(raw_t[raw_order], result.values[observed][raw_order]) if result.before else None
    return {
        "before": result.before,
        "before_error": result.before_error,
        "after": result.after,
        "after_values_on_original_timestamps": original_grid,
        "strict_target": result.target,
        "target_met": result.target_met,
        "input_rows": len(timestamps),
        "output_rows": len(result.timestamps),
        "inserted_rows": int(np.count_nonzero(result.source_index == -1)),
        "timestamps_changed": int(moved.sum()),
        "observed_missing_values_filled": int(np.count_nonzero(result.value_imputed & (result.source_index >= 0))),
        "observed_finite_values_changed": int(edited.sum()),
        "max_absolute_timestamp_shift": int(timestamp_changes.max()) if moved.any() else 0,
        "median_absolute_finite_value_change": float(np.median(value_changes)) if edited.any() else 0.0,
        "max_absolute_finite_value_change": float(value_changes.max()) if edited.any() else 0.0,
        "timestamp_repair": result.timestamp_details,
        "trend_model": result.trend_details,
    }


def write_csv(path: Path, result: RepairResult) -> None:
    with path.open("x", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow([
            "timestamp", "value", "source_index", "timestamp_modified", "value_imputed",
            "value_modified", "value_is_modeled_trend",
        ])
        writer.writerows(zip(
            result.timestamps, result.values, result.source_index,
            result.timestamp_modified.astype(np.uint8), result.value_imputed.astype(np.uint8),
            result.value_modified.astype(np.uint8), repeat(int(result.trend_details is not None)),
        ))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="CSV with integer timestamp and numeric value columns")
    parser.add_argument("output", type=Path, help="new CSV path for repaired points and row provenance")
    parser.add_argument("--nominal-interval", type=int, help="known sampling interval, in input timestamp units")
    parser.add_argument("--target", type=float, default=0.95, help="strict threshold for each score")
    parser.add_argument("--trend-model", action="store_true", help="allow separately flagged latent trend estimates")
    args = parser.parse_args()
    timestamps, values = [], []
    with args.input.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if not {"timestamp", "value"}.issubset(reader.fieldnames or ()):
            parser.error("input CSV must contain timestamp and value headers")
        for row in reader:
            try:
                timestamps.append(int(row["timestamp"]))
                cell = row["value"]
                values.append(float(cell) if cell and cell.strip() else float("nan"))
            except (TypeError, ValueError) as exc:
                parser.error(f"input line {reader.line_num}: {exc}")
    result = repair_series(
        timestamps, values, nominal_interval=args.nominal_interval, target=args.target,
        trend_model=args.trend_model,
    )
    write_csv(args.output, result)
    print(json.dumps(audit(result, timestamps, values), indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
