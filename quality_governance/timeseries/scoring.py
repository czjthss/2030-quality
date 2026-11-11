# SPDX-License-Identifier: Apache-2.0
# Adapted from Apache IoTDB TimeSeriesQuality for Python and NumPy.
# See NOTICE and LICENSES/Apache-2.0.txt.
"""Score numeric time series using the pinned IoTDB TimeSeriesQuality rules."""

import math
import warnings

import numpy as np

IOTDB_COMMIT = "fc6f68c213230d0bb166420f901f3bc8b00663ba"
METRICS = ("completeness", "consistency", "timeliness", "validity")
OUTLIER_COUNTS = ("value_outliers", "variation_outliers", "speed_outliers", "speedchange_outliers")
COUNTS = ("n", "missing", "special", "late", "redundant", *OUTLIER_COUNTS)
WINDOW_SIZE = 10


def _median(values):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return float(np.nanmedian(values))


def time_counts(timestamps):
    """Count missing, delayed and redundant samples from timestamp spacing."""
    base = _median(np.diff(timestamps))
    if not math.isfinite(base) or base <= 0:
        raise ValueError("nonpositive_or_nonfinite_median_interval")

    window = list(timestamps[:WINDOW_SIZE])
    next_index = len(window)
    missing = late = redundant = 0
    while len(window) > 1:
        gap = (window[1] - window[0]) / base
        if gap <= 0.5:
            window.pop(1)
            redundant += 1
        elif 2 <= gap <= 9:
            # IoTDB's default downtime setting excludes gaps beyond nine intervals.
            missing_slots = math.floor(gap - 1 + 0.5)
            found = 0
            index = 2
            while index < len(window):
                following_gap = (window[index] - window[index - 1]) / base
                if following_gap >= 2:
                    break
                if following_gap <= 0.5:
                    window.pop(index)
                    found += 1
                    if found == missing_slots:
                        break
                else:
                    index += 1
            late += found
            missing += missing_slots - found

        window.pop(0)
        while len(window) < WINDOW_SIZE and next_index < len(timestamps):
            window.append(timestamps[next_index])
            next_index += 1
    return base, missing, late, redundant


def _interpolate(timestamps, values):
    """Linearly fill the score's non-finite inputs, including at the endpoints."""
    filled = values.copy()
    filled[~np.isfinite(filled)] = np.nan
    anchors = np.flatnonzero(~np.isnan(filled))
    if len(anchors) < 2:
        raise ValueError("at_least_two_non_NaN_values_needed")

    def fill(left, right, start, stop):
        for index in range(start, stop):
            filled[index] = filled[left] + (
                (filled[right] - filled[left])
                * (timestamps[index] - timestamps[left])
                / (timestamps[right] - timestamps[left])
            )

    left, right = int(anchors[0]), int(anchors[1])
    with np.errstate(all="ignore"):
        fill(left, right, 0, right)
        for next_anchor in anchors[2:]:
            left, right = right, int(next_anchor)
            fill(left, right, left + 1, right)
        fill(left, right, right + 1, len(filled))
    return filled


def outliers(values):
    """Count deviations greater than three scaled median absolute deviations."""
    center = _median(values)
    with np.errstate(all="ignore"):
        residual = np.abs(values - center)
        scale = 1.4826 * _median(residual)
        return int(np.count_nonzero(residual > 3 * scale)), scale


def metric_counts(counts):
    """Return (error count, denominator) for each of the four score formulas."""
    size = counts["n"]
    missing = counts["missing"]
    return {
        "completeness": (missing + counts["special"], size + missing),
        "consistency": (counts["redundant"], size),
        "timeliness": (counts["late"], size),
        "validity": (sum(counts[key] for key in OUTLIER_COUNTS), 4 * size),
    }


def _score(errors, denominator):
    return 1 - errors / denominator if denominator else None


def evaluate(timestamps, values):
    """Score one sorted series and return the four scores and their counts."""
    timestamps = np.asarray(timestamps, dtype=float)
    values = np.asarray(values, dtype=float)
    if len(timestamps) <= WINDOW_SIZE:
        raise ValueError("UDF_ignores_windows_with_10_or_fewer_rows")
    gaps = np.diff(timestamps)
    if np.any(gaps < 0):
        raise ValueError("timestamps_not_sorted")

    filled = _interpolate(timestamps, values)
    base, missing, late, redundant = time_counts(timestamps)
    with np.errstate(all="ignore"):
        variation = np.diff(filled)
        speed = variation / gaps
        channels = tuple(outliers(feature) for feature in (filled, variation, speed, np.diff(speed)))

    counts = {
        "n": len(timestamps),
        "missing": missing,
        "special": int(np.count_nonzero(~np.isfinite(values))),
        "late": late,
        "redundant": redundant,
        **dict(zip(OUTLIER_COUNTS, (count for count, _ in channels))),
    }
    return {
        **counts,
        "base_us": base,
        **{name: _score(*pair) for name, pair in metric_counts(counts).items()},
        "zero_mad_channels": sum(scale == 0 for _, scale in channels),
        "duplicate_timestamps": int(np.count_nonzero(gaps == 0)),
    }


def pool(rows):
    """Sum error counts and denominators across independently scored series."""
    rows = list(rows)
    counts = {key: sum(row[key] for row in rows) for key in COUNTS}
    return {
        "series_count": len(rows),
        "devices": sorted({row["device"] for row in rows}),
        "input_point_cells": counts["n"],
        "counts": counts,
        **{
            name: {
                "errors": errors,
                "denominator": denominator,
                "passed": denominator - errors,
                "score": _score(errors, denominator),
            }
            for name, (errors, denominator) in metric_counts(counts).items()
        },
    }
