"""Optional continuous trend reconstruction for approximately periodic data."""

import numpy as np

from .value_repair import _validity


def reconstruct_trend(timestamps, values, observed, target):
    """Estimate a low-frequency signal and audit departures from the readings.

    The result is a modeled series at the existing timestamps. It retains the
    original value units and adds no noise. A generic correlation and edit
    budget can reject even a candidate that reaches the requested score.
    """
    interval = np.diff(timestamps).astype(float)
    score = _validity(values, interval)
    if score > target:
        return values, None
    finite_readings = values[observed]
    low, high = np.quantile(finite_readings, (0.05, 0.95))
    amplitude = high - low
    if not amplitude > 0 or not np.isfinite(amplitude):
        return values, None
    try:
        from scipy.interpolate import CubicSpline
        from scipy.signal import fftconvolve
    except ImportError as exc:
        raise RuntimeError("trend reconstruction requires SciPy") from exc

    # Fit on a uniformly spaced working grid, then evaluate at the same
    # repaired timestamps. Working grid values are never exported as samples.
    relative = (timestamps - timestamps[0]).astype(float)
    step = max(np.median(interval), relative[-1] / (8 * len(relative)))
    location = relative / step
    grid = np.arange(np.ceil(location[-1]) + 1)
    center = float(np.median(finite_readings))
    working_values = np.interp(grid, location, values - center)
    value_min, value_max = values.min(), values.max()

    best, details = values, None
    for width in (8, 16, 32, 64, 128, 256, 512, 1024):
        radius = 4 * width
        offsets = np.arange(-radius, radius + 1)
        weights = np.exp(-0.5 * (offsets / width) ** 2)
        weights /= weights.sum()
        padded = np.pad(working_values, radius, mode="edge")
        low_pass = fftconvolve(padded, weights, mode="valid")
        model = np.clip(CubicSpline(grid, low_pass)(location) + center, value_min, value_max)
        # Do not treat round-off on constant runs as a reconstructed signal.
        epsilon = 64 * np.finfo(float).eps * np.maximum(np.abs(values), amplitude)
        close = np.abs(model - values) <= epsilon
        model[close] = values[close]
        on_observed = model[observed]
        delta = np.abs(on_observed - finite_readings)
        median_delta, upper_delta = np.quantile(delta, (0.5, 0.95)) / amplitude
        if median_delta > 0.15 or upper_delta > 0.45 or np.ptp(on_observed) == 0:
            continue
        correlation = float(np.corrcoef(on_observed, finite_readings)[0, 1])
        if not correlation >= 0.85:
            continue
        candidate_score = _validity(model, interval)
        if candidate_score > score:
            best, score = model, candidate_score
            details = {
                "kernel_standard_deviation_in_timestamp_units": float(width * step),
                "median_edit_fraction_of_robust_amplitude": float(median_delta),
                "p95_edit_fraction_of_robust_amplitude": float(upper_delta),
                "correlation_to_previous_estimate": correlation,
            }
        if score > target:
            break
    return best, details
