"""Repair numeric values using shape-preserving interpolation and robust bands.

Fit independent bounds for values, differences, rates, and rate changes. Use
minimum-distance projections to reconcile their overlapping constraints. The
search is bounded and can return a result below the requested score.
"""

import numpy as np


def _exclude_spikes(values, segments):
    """Reject isolated level deviations from nearby observed values."""
    known = np.flatnonzero(np.isfinite(values))
    if len(known) < 7:
        return values
    observations = values[known]
    runs = segments[known]
    changes = np.diff(observations)[runs[:-1] == runs[1:]]
    changes = np.abs(changes[changes != 0])
    resolution = np.quantile(changes, 0.25) if len(changes) else 0.0
    offsets = np.r_[np.arange(-7, 0), np.arange(1, 8)]
    positions = np.arange(len(known))[:, None] + offsets
    positions = np.clip(positions, 0, len(known) - 1)
    neighborhood = observations[positions]
    nearby = (runs[positions] == runs[:, None]) & (positions != np.arange(len(known))[:, None])
    eligible = (nearby[:, :7].sum(axis=1) >= 3) & (nearby[:, 7:].sum(axis=1) >= 3)
    if not np.any(eligible):
        return values
    neighborhood = np.where(nearby[eligible], neighborhood[eligible], np.nan)
    center = np.nanmedian(neighborhood, axis=1)
    scale = 1.4826 * np.nanmedian(np.abs(neighborhood - center[:, None]), axis=1)
    difference = np.abs(observations[eligible] - center)
    # Keep the reference robust to a short block, and avoid treating the
    # smallest resolvable movement on a quantized plateau as a spike.
    outlying = difference > 5 * np.maximum(scale, resolution)
    cleaned = values.copy()
    cleaned[known[eligible][outlying]] = np.nan
    return cleaned if np.isfinite(cleaned).sum() >= 2 else values


def _interpolate(time, values, segments):
    """Use monotone cubic Hermite interpolation, with constant edge extension."""
    known = np.flatnonzero(np.isfinite(values))
    missing = np.flatnonzero(~np.isfinite(values))
    if len(known) < 2:
        raise ValueError("value repair needs two finite observations")
    result = values.copy()
    if not len(missing):
        return result

    anchor_time = time[known]
    anchor = values[known]
    interval = np.diff(anchor_time)
    slope = np.diff(anchor) / interval
    same_segment = np.diff(segments[known]) == 0
    derivative = np.zeros(len(known))
    # Harmonic-mean slopes stay within the local monotone shape and keep
    # constant intervals exactly flat. Segments separated by outages do not
    # inform one another's derivatives.
    pairs = (
        same_segment[:-1]
        & same_segment[1:]
        & (slope[:-1] != 0)
        & (slope[1:] != 0)
        & (np.signbit(slope[:-1]) == np.signbit(slope[1:]))
    )
    left_width, right_width = interval[:-1][pairs], interval[1:][pairs]
    weight_left = 2 * right_width + left_width
    weight_right = right_width + 2 * left_width
    derivative[1:-1][pairs] = (weight_left + weight_right) / (
        weight_left / slope[:-1][pairs] + weight_right / slope[1:][pairs]
    )
    # Use one-sided secants where the anchor ends its segment.
    left_end = same_segment & np.r_[True, ~same_segment[:-1]]
    right_end = same_segment & np.r_[~same_segment[1:], True]
    derivative[:-1][left_end] = slope[left_end]
    derivative[1:][right_end] = slope[right_end]

    right = np.searchsorted(known, missing)
    left = np.maximum(right - 1, 0)
    right = np.minimum(right, len(known) - 1)
    closer_right = time[known[right]] - time[missing] < time[missing] - time[known[left]]
    in_left_segment = segments[known[left]] == segments[missing]
    in_right_segment = segments[known[right]] == segments[missing]
    take_right = np.where(in_left_segment == in_right_segment, closer_right, in_right_segment)
    result[missing] = anchor[np.where(take_right, right, left)]

    between = (left != right) & in_left_segment & in_right_segment
    left, right = left[between], right[between]
    width = interval[left]
    pos = (time[missing[between]] - anchor_time[left]) / width
    smooth = pos * pos * (3 - 2 * pos)
    predicted = (
        anchor[left]
        + smooth * (anchor[right] - anchor[left])
        + pos * (1 - pos) * width
        * ((1 - pos) * derivative[left] - pos * derivative[right])
    )
    result[missing[between]] = np.clip(
        predicted, np.minimum(anchor[left], anchor[right]), np.maximum(anchor[left], anchor[right])
    )
    return result


def _features(values, interval):
    delta = np.diff(values)
    rate = delta / interval
    return values, delta, rate, np.diff(rate)


def _location_scale(feature):
    center = np.median(feature)
    residual = np.abs(feature - center)
    scale = 1.4826 * np.median(residual)
    return center, scale, residual


def _validity(values, interval):
    counts = 0
    for feature in _features(values, interval):
        if len(feature):
            _, scale, residual = _location_scale(feature)
            counts += np.count_nonzero(residual > 3 * scale)
    return 1 - counts / (4 * len(values))


def _constraints(values, interval, segments):
    dt = interval / np.median(interval)
    rate_weight = 1 / dt
    adjacent = segments[:-1] == segments[1:]
    consecutive = adjacent[:-1] & adjacent[1:]
    features = _features(values, dt)
    transitions = features[1][adjacent]
    repeated_levels = len(transitions) and 2 * np.count_nonzero(transitions == 0) >= len(transitions)
    level_mask = np.full(len(values), not repeated_levels)
    coefficients = (
        (np.ones(len(values)),),
        (-np.ones(len(dt)), np.ones(len(dt))),
        (-rate_weight, rate_weight),
        (rate_weight[:-1], -(rate_weight[:-1] + rate_weight[1:]), rate_weight[1:]),
    )
    for feature, valid, coeff in zip(
        features, (level_mask, adjacent, adjacent, consecutive), coefficients
    ):
        usable = feature[valid]
        if not len(usable):
            continue
        center, scale, residual = _location_scale(usable)
        if not scale:
            # Sparse legitimate changes can make MAD zero. A nonzero-scale
            # fallback allows real transitions rather than enforcing a plateau.
            nonzero = residual[residual > 0]
            if not len(nonzero):
                continue
            scale = 1.4826 * np.median(nonzero)
        for phase in range(len(coeff)):
            indexes = np.flatnonzero(valid[phase::len(coeff)]) * len(coeff) + phase
            if len(indexes):
                yield indexes, tuple(c[indexes] for c in coeff), center, scale


def _project(values, observed, constraints, width, sweeps):
    result = values.copy()
    # Missing values can move more readily than finite observations.
    inverse_weight = np.where(observed, 1.0, 2.0)
    groups = []
    for indexes, coeff, center, scale in constraints:
        offsets = tuple(indexes + i for i in range(len(coeff)))
        directions = tuple(c * inverse_weight[i] for c, i in zip(coeff, offsets))
        norm = sum(c * d for c, d in zip(coeff, directions))
        lo, hi = center - width * scale, center + width * scale
        groups.append((offsets, coeff, directions, norm, lo, hi, np.zeros(len(indexes))))
    # Dykstra projects onto intersecting slabs. Within each group the slabs
    # have disjoint supports and can be updated together.
    for _ in range(sweeps):
        for offsets, coeff, directions, norm, lo, hi, correction in groups:
            original = sum(c * result[i] for c, i in zip(coeff, offsets)) + correction * norm
            updated = (original - np.clip(original, lo, hi)) / norm
            shift = correction - updated
            correction[:] = updated
            for i, direction in zip(offsets, directions):
                result[i] += shift * direction
    # Restore differences at the scale of arithmetic roundoff.
    eps = 16 * np.finfo(float).eps * np.maximum(np.abs(values), np.abs(result))
    unchanged = np.abs(result - values) <= eps
    result[unchanged] = values[unchanged]
    return result


def repair_values(timestamps, values, *, target=0.95, segments=None):
    """Return one finite estimate per sorted, distinct input timestamp.

    NaN and infinities represent missing values. Missing values are interpolated
    within segments, with nearest observed value used at segment edges or in a
    segment with no observed value. When possible, accept the least restrictive
    candidate that meets the validity target. Each candidate starts from the
    same imputed values. If none passes, return the best valid candidate found.
    """
    timestamps = np.asarray(timestamps)
    values = np.asarray(values, dtype=float)
    if timestamps.ndim != 1 or values.shape != timestamps.shape or len(values) < 2:
        raise ValueError("timestamps and values must be matching one-dimensional arrays")
    if not 0 < target < 1:
        raise ValueError("target must be strictly between 0 and 1")
    time = np.asarray(timestamps - timestamps[0], dtype=float)
    interval = np.diff(time)
    if np.any(interval <= 0) or not np.all(np.isfinite(interval)):
        raise ValueError("value repair requires strictly increasing finite timestamps")
    segments = np.zeros(len(time), dtype=int) if segments is None else np.asarray(segments)
    if segments.shape != time.shape or np.any(segments[1:] < segments[:-1]):
        raise ValueError("segments must be ordered and match the timestamps")

    filled = _interpolate(time, values, segments)
    best = filled
    best_score = _validity(best, interval)
    if best_score > target:
        return best
    cleaned = _exclude_spikes(values, segments)
    observed = np.isfinite(cleaned)
    filled = _interpolate(time, cleaned, segments)
    sparse_score = _validity(filled, interval)
    if sparse_score > target:
        return filled
    if sparse_score > best_score:
        best, best_score = filled, sparse_score
    constraints = tuple(_constraints(filled, interval, segments))
    for width in (3.0, 2.5, 2.0, 1.5):
        candidate = _project(filled, observed, constraints, width, sweeps=12)
        if not np.all(np.isfinite(candidate)):
            continue
        score = _validity(candidate, interval)
        if score > target:
            return candidate
        if score > best_score:
            best, best_score = candidate, score
    return best
