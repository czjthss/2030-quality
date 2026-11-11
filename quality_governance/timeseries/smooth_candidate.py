"""Bounded deterministic denoising for readings with repeated sensor levels."""

import numpy as np

from .value_repair import _constraints, _project, _validity


def _within_runs(values, cuts, radius):
    """Estimate a local level with Gaussian weights, stopping at outage edges."""
    offsets = np.arange(-3 * radius, 3 * radius + 1)
    weights = np.exp(-0.5 * (offsets / radius) ** 2)
    weights /= weights.sum()
    output = values.copy()
    width = len(weights) // 2
    for start, stop in zip(cuts[:-1], cuts[1:]):
        run = values[start:stop]
        if len(run) <= 2 or run.min() == run.max():
            continue
        local = np.pad(run - run[0], width, mode="edge")
        output[start:stop] = np.convolve(local, weights, mode="valid") + run[0]
    eps = 16 * np.finfo(float).eps * np.abs(values)
    unchanged = np.abs(output - values) <= eps
    output[unchanged] = values[unchanged]
    return output


def refine_values(timestamps, values, observed, segments, target):
    """Try small, bounded changes to the repaired readings.

    This pass estimates a locally smooth latent level. It creates no new
    timestamps and leaves constant runs intact. A candidate can be selected
    only when its 95th-percentile change on original finite readings is at most
    10% of their robust amplitude after the initial repair.
    """
    interval = np.diff(timestamps).astype(float)
    best_score = _validity(values, interval)
    if best_score > target:
        return values
    typical_values = values[observed]
    low, high = np.quantile(typical_values, (0.05, 0.95))
    edit_limit = 0.10 * (high - low)
    if not edit_limit > 0 or not np.isfinite(edit_limit):
        return values

    cuts = np.r_[0, np.flatnonzero(np.diff(segments)) + 1, len(values)]
    best = values
    for radius in (2, 4, 8, 16, 32, 64):
        smoothed = _within_runs(values, cuts, radius)
        for project in (False, True):
            candidate = smoothed
            if project:
                constraints = tuple(_constraints(smoothed, interval, segments))
                candidate = _project(smoothed, observed, constraints, 2.0, sweeps=8)
            if not np.all(np.isfinite(candidate)):
                continue
            delta = np.abs(candidate[observed] - typical_values)
            if np.count_nonzero(delta > edit_limit) > 0.05 * len(delta):
                continue
            candidate_score = _validity(candidate, interval)
            if candidate_score > target:
                return candidate
            if candidate_score > best_score:
                best, best_score = candidate, candidate_score
    constraints = tuple(_constraints(best, interval, segments))
    for width in (2.0, 1.5, 1.0):
        candidate = _project(best, observed, constraints, width, sweeps=18)
        if not np.all(np.isfinite(candidate)):
            continue
        delta = np.abs(candidate[observed] - typical_values)
        if np.count_nonzero(delta > edit_limit) > 0.05 * len(delta):
            continue
        candidate_score = _validity(candidate, interval)
        if candidate_score > target:
            return candidate
        if candidate_score > best_score:
            best, best_score = candidate, candidate_score
    return best
