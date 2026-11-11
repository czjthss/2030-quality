"""Infer missing slots and separate collisions in approximately periodic series."""

from dataclasses import dataclass
import math

import numpy as np


@dataclass(frozen=True)
class TimestampRepair:
    timestamps: np.ndarray
    source_index: np.ndarray
    shifted: np.ndarray
    period: float
    segments: np.ndarray
    metadata: dict


def _estimate_period(gaps):
    """Use the most populated cluster of positive gaps on a relative scale."""
    positive = gaps[gaps > 0]
    if not len(positive):
        raise ValueError("cannot infer a sampling interval without distinct timestamps")

    # Neighboring quarter-octave bins cover small clock jitter without grouping
    # a nominal interval with twice that interval (one missing sample).
    bins = np.floor(4 * np.log2(positive)).astype(int)
    counts = np.bincount(bins)
    peak = int(np.argmax(np.convolve(counts, np.ones(3), mode="full"))) - 1
    return float(np.median(positive[np.abs(bins - peak) <= 1]))


def _alternate_cadence(gaps, period):
    """Find sustained, regular spacing outside the main cadence's jitter band."""
    alternative = (gaps > 0) & ((gaps < 0.65 * period) | (gaps >= 1.5 * period))
    similar = np.maximum(gaps[:-1], gaps[1:]) <= 1.15 * np.minimum(gaps[:-1], gaps[1:])
    cuts = np.r_[0, np.flatnonzero(~(alternative[:-1] & alternative[1:] & similar)) + 1, len(gaps)]
    sustained = np.zeros(len(gaps), dtype=bool)
    for i in np.flatnonzero(np.diff(cuts) >= 16):
        start, stop = cuts[i:i + 2]
        run = gaps[start:stop]
        if np.max(run) <= 1.25 * np.min(run):
            sustained[start:stop] = True
    return sustained


def _recover_neighbor_slots(timestamps, period, fixed):
    """Move an isolated near-collision into its adjacent vacant slot, if any."""
    minimum = max(1, math.ceil(0.65 * period))
    nominal = max(1, round(period))
    gaps = np.diff(timestamps)
    previous, following = gaps[:-1], gaps[1:]
    # Prefer the earlier slot for an observed long-gap / short-gap pair.
    # Reverse the direction for short-gap / long-gap pairs without that option.
    mutable = ~(fixed[:-1] | fixed[1:])
    backed = mutable & (previous >= 1.75 * period) & (following < minimum)
    slots = np.flatnonzero(backed) + 1
    timestamps[slots] -= np.minimum(nominal - gaps[slots], gaps[slots - 1] - minimum)

    gaps = np.diff(timestamps)
    previous, following = gaps[:-1], gaps[1:]
    advanced = mutable & (previous < minimum) & (following >= 1.75 * period)
    slots = np.flatnonzero(advanced) + 1
    timestamps[slots] += np.minimum(nominal - gaps[slots - 1], gaps[slots] - minimum)


def _separate_collisions(timestamps, minimum, lower, upper):
    """Project onto minimum-separated times using forward and backward envelopes."""
    if len(timestamps) <= 1 or np.all(np.diff(timestamps) >= minimum):
        return timestamps
    if (len(timestamps) - 1) * minimum > np.iinfo(np.int64).max:
        raise ValueError("minimum timestamp separation would exceed the supported time span")
    ramp = np.arange(len(timestamps), dtype=np.int64) * minimum
    baseline = timestamps - timestamps[0]
    offsets = baseline - ramp
    forward = np.maximum.accumulate(offsets)
    backward = np.minimum.accumulate(offsets[::-1])[::-1]
    # Integer midpoint without overflowing when the two envelopes have large offsets.
    middle = forward // 2 + backward // 2 + ((forward % 2 + backward % 2) // 2)
    anchor = int(timestamps[0])
    floor = lower - anchor
    ceiling = upper - anchor - int(ramp[-1])
    if int(middle[0]) < floor:
        middle = np.maximum(middle, floor)
    if int(middle[-1]) > ceiling:
        middle = np.minimum(middle, ceiling)
    return middle + ramp + anchor


def _absolute_distances(left, right):
    """Return exact integer distances even for subsecond shifts on epoch ticks."""
    return np.maximum(left, right).astype(np.uint64) - np.minimum(left, right).astype(np.uint64)


def repair_timestamps(timestamps, *, nominal_interval=None, gap_limit_periods=9.0):
    """Return repaired ticks and a mapping to every original observation.

    The input is a one-dimensional sequence of integer ticks. Cadence is
    estimated unless provided explicitly in the same unit. An interval longer
    than ``gap_limit_periods`` cadences separates runs and is left unfilled.
    With inferred cadence, sustained different spacing is left intact.
    Each inferred slot has source index -1. A moved timestamp is an estimate.
    """
    raw = np.asarray(timestamps)
    if raw.ndim != 1 or raw.size < 2 or raw.dtype.kind not in "iu":
        raise ValueError("timestamps must contain at least two integer ticks")
    maximum = np.iinfo(np.int64).max
    if raw.dtype.kind == "u" and int(np.max(raw)) > maximum:
        raise ValueError("timestamps must be representable as int64")
    raw = raw.astype(np.int64, copy=False)
    order = np.argsort(raw, kind="stable")
    original = raw[order]
    if int(original[-1]) - int(original[0]) > maximum:
        raise ValueError("timestamp span exceeds the supported int64 range")

    original_gaps = np.diff(original)
    period = _estimate_period(original_gaps) if nominal_interval is None else float(nominal_interval)
    if not math.isfinite(period) or period < 1 or period > maximum:
        raise ValueError("nominal_interval must be at least one tick and fit in int64")
    if not math.isfinite(gap_limit_periods) or not 2 <= gap_limit_periods <= 100:
        raise ValueError("gap_limit_periods must lie between 2 and 100")
    gap_limit = gap_limit_periods * period
    outages = original_gaps > gap_limit
    alternate = _alternate_cadence(original_gaps, period) if nominal_interval is None else np.zeros_like(outages)
    fixed = outages | alternate

    anchors = original.copy()
    _recover_neighbor_slots(anchors, period, fixed)
    gaps = np.diff(anchors)
    eligible = ~fixed & (gaps >= 1.5 * period) & (gaps <= gap_limit)
    subdivisions = np.ones(len(gaps), dtype=np.int64)
    subdivisions[eligible] = np.floor(gaps[eligible] / period + 0.5).astype(np.int64)
    anchor_positions = np.r_[0, np.cumsum(subdivisions)]
    count = int(anchor_positions[-1]) + 1
    repaired = np.empty(count, dtype=np.int64)
    source_index = np.full(count, -1, dtype=np.int64)
    repaired[anchor_positions] = anchors
    source_index[anchor_positions] = order
    if count > len(original):
        left = np.repeat(np.arange(len(gaps)), subdivisions)
        inserted = np.flatnonzero(source_index < 0)
        intervals = left[inserted]
        steps = inserted - anchor_positions[intervals]
        repaired[inserted] = anchors[intervals] + np.rint(
            (gaps[intervals] / subdivisions[intervals]) * steps
        ).astype(np.int64)

    cuts = np.r_[0, anchor_positions[1:][outages], count]
    segments = np.repeat(np.arange(len(cuts) - 1, dtype=np.int64), np.diff(cuts))
    minimum = max(1, math.ceil(0.65 * period))
    if np.any(alternate & (original_gaps < minimum)):
        # Treat a sustained faster regime as genuine timing variation.
        minimum = 1
    outage_positions = cuts[1:-1]
    earlier = repaired[outage_positions - 1]
    later = repaired[outage_positions]
    # Reserve the inferred outage. Only a collision at a run boundary may
    # borrow room on that side, never past the midpoint of the outage.
    slack = np.floor((later - earlier - gap_limit) / 2).astype(np.int64)
    midpoints = earlier + (later - earlier) // 2
    minimum_tick = np.iinfo(np.int64).min
    lower_bounds = np.r_[minimum_tick, later - slack]
    upper_bounds = np.r_[earlier + slack, maximum]
    fallback_lower = np.r_[minimum_tick, midpoints + 1]
    fallback_upper = np.r_[midpoints, maximum]
    restricted = 0
    limited = 0
    for i, (start, stop) in enumerate(zip(cuts[:-1], cuts[1:])):
        width = int(stop - start - 1)
        if width == 0:
            continue
        lower, upper = int(lower_bounds[i]), int(upper_bounds[i])
        if upper - lower < width:
            lower, upper = int(fallback_lower[i]), int(fallback_upper[i])
        local_minimum = min(minimum, (upper - lower) // width)
        if local_minimum < 1:
            raise ValueError("insufficient timestamp precision to separate all observations in a run")
        restricted += local_minimum < minimum
        run = repaired[start:stop]
        separated = _separate_collisions(run, local_minimum, lower, upper)
        if np.any(_absolute_distances(separated, run) > period):
            # Large displacement could convert genuine high-frequency sampling
            # into an invented cadence. Only disambiguate exact collisions.
            separated = _separate_collisions(run, 1, lower, upper)
            limited += 1
        repaired[start:stop] = separated
    if np.any(repaired[1:] <= repaired[:-1]):
        raise ValueError("cannot separate the observations without overlapping disconnected runs")

    shifted = np.zeros(count, dtype=bool)
    shifted[anchor_positions] = repaired[anchor_positions] != original
    absolute_shift = _absolute_distances(repaired[anchor_positions], original)
    metadata = {
        "nominal_interval": period,
        "nominal_interval_source": "estimated" if nominal_interval is None else "provided",
        "gap_limit_periods": float(gap_limit_periods),
        "inferred_slots": count - len(original),
        "shifted_observations": int(np.count_nonzero(shifted)),
        "max_absolute_timestamp_shift": int(np.max(absolute_shift)),
        "retained_outages": int(np.count_nonzero(
            _absolute_distances(repaired[outage_positions], repaired[outage_positions - 1]) > gap_limit
        )),
        "runs_with_restricted_separation": restricted,
        "runs_with_limited_timestamp_shifts": limited,
        "preserved_alternate_cadence_intervals": int(np.count_nonzero(alternate)),
    }
    return TimestampRepair(repaired, source_index, shifted, period, segments, metadata)
