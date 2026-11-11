"""Check provenance, exact scoring and repair against synthetic ground truth."""

from dataclasses import replace
import csv
from datetime import datetime, timedelta
import gzip
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

import numpy as np

from quality_governance.timeseries.repair import audit, repair_series
from quality_governance.timeseries.scoring import METRICS, evaluate, metric_counts, pool
from quality_governance.timeseries.trend_model import reconstruct_trend
from quality_governance.timeseries.value_repair import _validity


def reference_series(count, cadence):
    indexes = np.arange(count, dtype=np.int64)
    values = 18 + 2 * np.sin(2 * np.pi * indexes / 45) + 0.7 * np.cos(2 * np.pi * indexes / 91)
    return indexes * cadence, values


class PortableRepairTest(unittest.TestCase):
    def test_pool_uses_error_counts_and_preserves_metric_denominators(self):
        timestamps = np.r_[0:7, 8:24]
        values = timestamps.astype(float)
        values[3] = np.nan
        incomplete = evaluate(timestamps, values)
        complete = evaluate(np.arange(40), np.arange(40))
        self.assertEqual(incomplete["missing"], 1)
        self.assertEqual(incomplete["special"], 1)
        self.assertEqual(metric_counts(incomplete)["completeness"], (2, 24))
        combined = pool([
            {"device": 1, **incomplete},
            {"device": 2, **complete},
        ])
        self.assertEqual(combined["completeness"]["score"], 1 - 2 / 64)
        self.assertEqual(combined["consistency"]["denominator"], 63)
        self.assertEqual(combined["timeliness"]["denominator"], 63)
        self.assertEqual(combined["validity"]["denominator"], 4 * 63)

    @unittest.skipUnless(importlib.util.find_spec("scipy"), "optional trend mode needs SciPy")
    def test_trend_model_reduces_quantized_oscillation(self):
        index = np.arange(4096)
        timestamps = index * 17 + 10**18
        truth = 10 + 3 * np.sin(2 * np.pi * index / 512)
        readings = np.round(truth + 0.4 * np.sin(2 * np.pi * index / 3))
        observed = np.ones(len(readings), dtype=bool)
        interval = np.diff(timestamps).astype(float)
        model, details = reconstruct_trend(timestamps, readings, observed, 0.95)
        self.assertIsNotNone(details)
        self.assertLess(_validity(readings, interval), 0.95)
        self.assertGreater(_validity(model, interval), 0.95)
        self.assertLess(np.mean((model - truth) ** 2), np.mean((readings - truth) ** 2))
        self.assertGreaterEqual(details["correlation_to_previous_estimate"], 0.85)

    def assert_provenance(self, timestamps, values, result):
        source_index = result.source_index
        observed = source_index >= 0
        inserted = ~observed
        self.assertTrue(np.all(source_index[inserted] == -1))
        np.testing.assert_array_equal(np.sort(source_index[observed]), np.arange(len(timestamps)))
        self.assertEqual(result.timestamps.dtype, np.dtype(np.int64))
        self.assertTrue(np.all(np.diff(result.timestamps) > 0))
        self.assertTrue(np.isfinite(result.values).all())

        expected_timestamp_changed = np.zeros(len(source_index), dtype=bool)
        original_ticks = np.asarray(timestamps, dtype=np.int64)
        expected_timestamp_changed[observed] = result.timestamps[observed] != original_ticks[source_index[observed]]
        np.testing.assert_array_equal(result.timestamp_modified, expected_timestamp_changed)

        original_values = np.full(len(source_index), np.nan)
        original_values[observed] = values[source_index[observed]]
        expected_value_imputed = ~np.isfinite(original_values)
        np.testing.assert_array_equal(result.value_imputed, expected_value_imputed)
        expected_value_modified = ~expected_value_imputed & (result.values != original_values)
        np.testing.assert_array_equal(result.value_modified, expected_value_modified)

        independent_after = evaluate(result.timestamps - result.timestamps[0], result.values)
        self.assertEqual(result.after, independent_after)
        report = audit(result, timestamps, values)
        self.assertEqual(report['inserted_rows'], int(inserted.sum()))
        self.assertEqual(report['observed_finite_values_changed'], int(expected_value_modified.sum()))
        self.assertEqual(report['timestamps_changed'], int(expected_timestamp_changed.sum()))

    def test_clean_series_and_high_epoch_are_preserved(self):
        ticks, values = reference_series(180, 17)
        timestamps = ticks + 10**18
        order = np.random.default_rng(1042).permutation(len(ticks))
        result = repair_series(timestamps[order], values[order])
        self.assert_provenance(timestamps[order], values[order], result)
        np.testing.assert_array_equal(result.timestamps, timestamps)
        np.testing.assert_array_equal(result.values, values)
        self.assertEqual(result.before, result.after)
        self.assertTrue(result.target_met)
        self.assertFalse(replace(result, target=min(result.after[m] for m in METRICS)).target_met)

    def test_unsigned_high_epoch_reports_exact_timestamp_shift(self):
        cadence = 17
        timestamps = np.arange(48, dtype=np.uint64) * cadence + np.uint64(10**18)
        timestamps[20] = timestamps[21]
        values = np.arange(len(timestamps), dtype=float)
        result = repair_series(timestamps, values)
        self.assert_provenance(timestamps, values, result)
        report = audit(result, timestamps, values)
        self.assertEqual(report['timestamps_changed'], 1)
        self.assertEqual(report['max_absolute_timestamp_shift'], cadence)
        self.assertEqual(result.timestamp_details['max_absolute_timestamp_shift'], cadence)

    def test_combined_defects_with_known_ground_truth(self):
        # The three regions create missing samples, delayed pairs, and short
        # intervals respectively. Their separation lets the evaluator count
        # all three defects independently.
        cadence = 120
        ticks, truth = reference_series(720, cadence)
        indexes = np.arange(len(ticks))
        retained = ~((indexes < 230) & ((indexes % 10 == 5) | (indexes % 10 == 6)))
        retained_indexes = indexes[retained]
        timestamps = ticks[retained].copy()
        delayed = np.flatnonzero((retained_indexes > 245) & (retained_indexes < 470) & (retained_indexes % 4 == 2))
        timestamps[delayed] = timestamps[delayed + 1]
        crowded = np.flatnonzero((retained_indexes > 485) & (retained_indexes < 711) & (retained_indexes % 4 == 2))
        timestamps[crowded] = timestamps[crowded + 1] - 10

        rng = np.random.default_rng(84652)
        corrupted = truth.copy()
        spikes = rng.choice(np.r_[0:175, 180:len(ticks)], size=55, replace=False)
        corrupted[spikes] += rng.choice([-15, 15], size=len(spikes))
        corrupted[175:180] += 12
        values = corrupted[retained].copy()
        values[20:24] = [np.nan, np.inf, -np.inf, np.nan]
        original_timestamps, original_values = timestamps.copy(), values.copy()

        result = repair_series(timestamps, values)
        self.assert_provenance(timestamps, values, result)
        np.testing.assert_array_equal(timestamps, original_timestamps)
        np.testing.assert_array_equal(values, original_values)
        self.assertTrue(all(result.before[m] < 0.95 for m in METRICS), result.before)
        self.assertTrue(result.target_met, result.after)

        observed = result.source_index >= 0
        source_index = result.source_index[observed]
        original_indexes = retained_indexes[source_index]
        expected_timestamps = ticks[original_indexes]
        before_timing_error = np.mean(np.abs(timestamps[source_index] - expected_timestamps))
        after_timing_error = np.mean(np.abs(result.timestamps[observed] - expected_timestamps))
        self.assertLess(after_timing_error, 0.25 * before_timing_error)
        inserted = result.timestamps[~observed]
        np.testing.assert_array_equal(inserted, ticks[~retained])

        known_values = truth[original_indexes]
        measured_values = values[source_index]
        finite = np.isfinite(measured_values)
        before_rmse = np.sqrt(np.mean((measured_values[finite] - known_values[finite]) ** 2))
        after_values = result.values[observed]
        after_rmse = np.sqrt(np.mean((after_values[finite] - known_values[finite]) ** 2))
        self.assertLess(after_rmse, 0.75 * before_rmse)
        initially_clean = finite & (measured_values == known_values)
        self.assertLess(np.sqrt(np.mean((after_values[initially_clean] - known_values[initially_clean]) ** 2)), 0.6)

    def test_nominal_cadence_and_long_downtime(self):
        cadence = 13
        ticks = np.r_[0:40, 150:190] * cadence - 1200
        retained = np.ones(len(ticks), dtype=bool)
        retained[12:14] = False
        timestamps = ticks[retained]
        values = np.full(len(timestamps), 7.25)
        values[[0, 23, -1]] = [np.nan, np.inf, -np.inf]

        result = repair_series(timestamps, values, nominal_interval=cadence)
        self.assert_provenance(timestamps, values, result)
        np.testing.assert_array_equal(result.timestamps, ticks)
        np.testing.assert_array_equal(result.values, np.full(len(ticks), 7.25))
        self.assertEqual(result.timestamp_details['nominal_interval'], cadence)
        self.assertEqual(result.after['zero_mad_channels'], 4)
        self.assertTrue(result.target_met)

    def test_irregular_healthy_observations_are_deterministic(self):
        timestamps, values = reference_series(160, 203)
        rng = np.random.default_rng(93721)
        timestamps += rng.integers(-18, 19, size=len(timestamps))
        first = repair_series(timestamps, values)
        second = repair_series(timestamps, values)
        self.assert_provenance(timestamps, values, first)
        self.assertTrue(first.target_met, first.after)
        self.assertEqual(first.before, second.before)
        self.assertEqual(first.after, second.after)
        np.testing.assert_array_equal(first.timestamps, timestamps)
        np.testing.assert_array_equal(first.timestamps, second.timestamps)
        np.testing.assert_array_equal(first.values, second.values)

    def test_sustained_cadence_switch_preserves_observations(self):
        timestamps = np.r_[np.arange(300) * 1000, 300000 + np.arange(300) * 4000]
        values = np.full(len(timestamps), 7.25)
        result = repair_series(timestamps, values)
        self.assert_provenance(timestamps, values, result)
        np.testing.assert_array_equal(result.timestamps, timestamps)
        np.testing.assert_array_equal(result.values, values)
        self.assertLess(result.after['completeness'], 0.95)
        self.assertFalse(result.target_met)

    def test_invalid_or_unidentifiable_inputs(self):
        timestamps = np.arange(24)
        values = np.ones(len(timestamps))
        with self.assertRaises(ValueError):
            repair_series(timestamps.astype(float), values)
        with self.assertRaises(ValueError):
            repair_series(timestamps, values[:-1])
        with self.assertRaises(ValueError):
            repair_series(timestamps[:10], values[:10])
        with self.assertRaises(ValueError):
            repair_series(timestamps, np.full(len(timestamps), np.nan))
        with self.assertRaises(ValueError):
            repair_series(np.zeros_like(timestamps), values)
        with self.assertRaises(ValueError):
            repair_series(timestamps, values, nominal_interval=0)
        with self.assertRaises(ValueError):
            repair_series(timestamps, values, target=1)

    def test_cli_preserves_source_and_exports_provenance(self):
        with TemporaryDirectory() as temporary:
            input_path = Path(temporary) / 'source.csv'
            output_path = Path(temporary) / 'repaired.csv'
            rows = ['timestamp,value\n']
            for index in range(48):
                if index in (12, 13):
                    continue
                cell = '' if index == 20 else str(3 * index + 5)
                rows.append(f'{index * 79},{cell}\n')
            original = ''.join(rows)
            input_path.write_text(original, encoding='utf-8')
            command = [sys.executable, '-m', 'quality_governance.timeseries.repair', str(input_path), str(output_path),
                       '--nominal-interval', '79']
            completed = subprocess.run(command, cwd=Path(__file__).resolve().parents[1], check=True,
                                       capture_output=True, text=True)
            report = json.loads(completed.stdout)
            self.assertTrue(report['target_met'])
            self.assertEqual(report['inserted_rows'], 2)
            self.assertEqual(report['observed_missing_values_filled'], 1)
            self.assertEqual(input_path.read_text(encoding='utf-8'), original)
            with output_path.open(encoding='utf-8', newline='') as stream:
                repaired = list(csv.DictReader(stream))
            self.assertEqual(len(repaired), 48)
            self.assertEqual([int(row['timestamp']) for row in repaired], [i * 79 for i in range(48)])
            self.assertEqual([float(row['value']) for row in repaired], [3 * i + 5 for i in range(48)])
            self.assertEqual([i for i, row in enumerate(repaired) if row['source_index'] == '-1'], [12, 13])

    def test_benchmark_cli_scores_its_input_without_a_frozen_baseline(self):
        with TemporaryDirectory() as temporary:
            input_path = Path(temporary) / "data.txt.gz"
            report_path = Path(temporary) / "report.json"
            start = datetime(2004, 3, 1)
            with gzip.open(input_path, "wt", encoding="utf-8") as stream:
                for device_id, size in ((1, 12), (2, 3)):
                    for index in range(size):
                        stamp = (start + timedelta(seconds=31 * index)).isoformat(sep=" ")
                        stream.write(f"{stamp} {index} {device_id} 17 43 85 3.1\n")
            subprocess.run(
                [
                    sys.executable, "-m", "quality_governance.timeseries.benchmark",
                    "--input", str(input_path), "--report", str(report_path),
                ],
                cwd=Path(__file__).resolve().parents[1], check=True, capture_output=True, text=True,
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["parser_counts"], {"physical_rows": 15, "unassignable_rows": 0})
            self.assertEqual(report["summary"]["series_count"], 4)
            self.assertEqual(report["summary"]["before"]["input_point_cells"], 48)
            self.assertEqual(report["summary"]["after"]["completeness"]["score"], 1)
            self.assertEqual(report["failures"], [])
            self.assertEqual(len(report["original_unscorable_series"]), 4)


if __name__ == '__main__':
    unittest.main()
