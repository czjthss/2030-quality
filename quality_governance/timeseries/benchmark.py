"""Run the repair and IoTDB-based evaluation on the Intel Lab log."""

import argparse
from array import array
from collections import defaultdict
from datetime import datetime, timedelta
import gzip
import json
from pathlib import Path
from time import monotonic

import numpy as np

from .repair import audit, repair_series, score, write_csv
from .scoring import COUNTS, IOTDB_COMMIT, METRICS, pool

VARIABLES = ("temperature", "humidity", "light", "voltage")
EPOCH = datetime(1970, 1, 1)
CHANGE_COUNTS = (
    "input_rows", "output_rows", "inserted_rows", "timestamps_changed",
    "observed_missing_values_filled", "observed_finite_values_changed",
)


def load_series(path, device=None):
    """Read the gzip sensor log into one group per parseable device ID."""
    groups = defaultdict(lambda: [array("q"), *(array("d") for _ in VARIABLES)])
    parser_counts = {"physical_rows": 0, "unassignable_rows": 0}
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            parser_counts["physical_rows"] += 1
            fields = line.rstrip("\r\n").split(" ")
            try:
                if len(fields) != 8:
                    raise ValueError("unexpected field count")
                device_id = int(fields[3])
                elapsed = datetime.fromisoformat(f"{fields[0]}T{fields[1]}") - EPOCH
                timestamp = elapsed // timedelta(microseconds=1)
                values = [float(value) if value else float("nan") for value in fields[4:]]
            except (ValueError, OverflowError):
                parser_counts["unassignable_rows"] += 1
                continue
            if device is None or device_id == device:
                for column, value in zip(groups[device_id], (timestamp, *values)):
                    column.append(value)
    return groups, parser_counts


def summarize(rows):
    """Pool the original scores, repaired scores and original-grid rescores."""
    if not rows:
        return None
    pooled = {
        name: pool({"device": row["device"], **row[name]} for row in rows)
        for name in ("before", "after", "after_values_on_original_timestamps")
    }
    return {
        **pooled,
        "series_strictly_above_target": sum(row["target_met"] for row in rows),
        "series_count": len(rows),
        "series_using_trend_model": sum(row["trend_model"] is not None for row in rows),
        "pooled_strictly_above_target": all(
            pooled["after"][name]["score"] > rows[0]["strict_target"] for name in METRICS
        ),
        **{name: sum(row[name] for row in rows) for name in CHANGE_COUNTS},
    }


def _excluded_series(label, timestamps, values, reason):
    return {
        **label,
        "start": (EPOCH + timedelta(microseconds=int(timestamps.min()))).isoformat(),
        "end": (EPOCH + timedelta(microseconds=int(timestamps.max()))).isoformat(),
        "n": len(timestamps),
        "special": int(np.count_nonzero(~np.isfinite(values))),
        "reason": reason,
    }


def run_benchmark(input_path, *, device=None, variable=None, trend_model=False, export=None, baseline=None):
    """Return a benchmark report, optionally checking the frozen original audit."""
    started = monotonic()
    groups, parser_counts = load_series(input_path, device)
    print("Parsed", parser_counts, "device groups", len(groups), flush=True)

    baseline_rows = {}
    baseline_excluded = {}
    version = {"sha": IOTDB_COMMIT}
    if baseline is not None:
        version = baseline["commit"]
        if version["sha"] != IOTDB_COMMIT:
            raise ValueError("baseline uses a different IoTDB commit")
        if any(parser_counts[key] != baseline["parser"][key] for key in parser_counts):
            raise ValueError("input row counts disagree with the frozen evaluation scope")
        baseline_rows = {(row["device"], row["variable"]): row for row in baseline["series"]}
        baseline_excluded = {(row["device"], row["variable"]): row for row in baseline["failed_series"]}
        expected_keys = {
            key for key in baseline_rows.keys() | baseline_excluded.keys()
            if (device is None or key[0] == device) and (variable is None or key[1] == variable)
        }
        actual_keys = {(device_id, name) for device_id in groups for name in VARIABLES if variable in (None, name)}
        if actual_keys != expected_keys:
            raise ValueError("input device-variable series disagree with the frozen evaluation scope")

    rows, failures, excluded = [], [], []
    for device_id, columns in sorted(groups.items()):
        timestamps = np.asarray(columns[0], dtype=np.int64)
        for name, column in zip(VARIABLES, columns[1:]):
            if variable not in (None, name):
                continue
            label = {"device": device_id, "variable": name}
            key = (device_id, name)
            if key in baseline_excluded:
                excluded.append(baseline_excluded[key])
                continue

            values = np.asarray(column, dtype=np.float64)
            try:
                result = repair_series(timestamps, values, trend_model=trend_model)
            except (ValueError, RuntimeError) as exc:
                if baseline is None:
                    order = np.argsort(timestamps, kind="stable")
                    try:
                        score(timestamps[order], values[order])
                    except ValueError as scoring_error:
                        excluded.append(_excluded_series(label, timestamps, values, str(scoring_error)))
                        continue
                failures.append({**label, "input_rows": len(values), "error": str(exc)})
                continue
            if result.before is None and baseline is None:
                excluded.append(_excluded_series(label, timestamps, values, result.before_error))
                continue
            if baseline is not None and (
                result.before is None or any(result.before[field] != baseline_rows[key][field] for field in COUNTS)
            ):
                failures.append({**label, "input_rows": len(values), "error": "raw counts disagree with baseline"})
                continue

            rows.append({**label, **audit(result, timestamps, values)})
            if export is not None:
                write_csv(export, result)
            if device is not None or len(rows) % 20 == 0:
                print("Scored", len(rows), label, {metric: round(result.after[metric], 5) for metric in METRICS},
                      flush=True)

    per_variable = {
        name: summarize([row for row in rows if row["variable"] == name])
        for name in VARIABLES
    }
    return {
        "input": str(input_path),
        "filters": {"device": device, "variable": variable},
        "iotdb_version": version,
        "trend_model_enabled": trend_model,
        "parser_counts": parser_counts,
        "summary": summarize(rows),
        "per_variable": per_variable,
        "every_reported_variable_strictly_above_target": bool(rows) and not failures and all(
            entry["pooled_strictly_above_target"] for entry in per_variable.values() if entry is not None
        ),
        "series": rows,
        "failures": failures,
        "original_unscorable_series": excluded,
        "elapsed_seconds": monotonic() - started,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Intel Lab data.txt.gz")
    parser.add_argument("--report", type=Path, required=True, help="new JSON report path")
    parser.add_argument("--device", type=int)
    parser.add_argument("--variable", choices=VARIABLES)
    parser.add_argument("--export", type=Path, help="new CSV path for the selected device and variable")
    parser.add_argument("--trend-model", action="store_true", help="allow separately flagged trend estimates")
    parser.add_argument("--baseline", type=Path, help="optional full_intel_scores.json from the original audit")
    args = parser.parse_args()
    if args.export is not None and (args.device is None or args.variable is None):
        parser.error("--export requires --device and --variable")
    destinations = [path for path in (args.report, args.export) if path is not None]
    if len({path.resolve() for path in destinations}) != len(destinations):
        parser.error("--report and --export must have distinct paths")
    for path in destinations:
        if path.exists():
            parser.error(f"choose a new output path: {path}")
    baseline = json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline is not None else None
    report = run_benchmark(
        args.input, device=args.device, variable=args.variable, trend_model=args.trend_model,
        export=args.export, baseline=baseline,
    )
    with args.report.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write("\n")
    if report["summary"] is not None:
        scores = report["summary"]["after"]
        print("Pooled", {name: round(scores[name]["score"], 6) for name in METRICS}, flush=True)
    if not report["series"] or report["failures"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
