"""Exercise MAUDE rules and the final export with small synthetic master ZIPs."""

import csv
import gzip
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from quality_governance.relational.dates import TIMELY, date_status, parse_date
from quality_governance.relational.maude import REPORT_NAME, SCOPE_COLUMNS, export_subset
from quality_governance.relational.reader import SCORE_COLUMNS
from quality_governance.relational.scoring import Counts, count_report, metric

SCORE_INDEXES = (3, 7, 49, 31, 32, 29, 30)
HEADER = [f"UNUSED_{index}" for index in range(86)]
HEADER[0] = "MDR_REPORT_KEY"
for index, name in zip(SCORE_INDEXES, SCORE_COLUMNS):
    HEADER[index] = name
HEADER[-len(SCOPE_COLUMNS):] = SCOPE_COLUMNS
SCOPE_INDEXES = tuple(HEADER.index(name) for name in SCOPE_COLUMNS)


def report(
    key, *, received="01/31/2026", awareness="01/01/2026",
    kind="Thirty-Day", phase="I", summary="N", exemption="", number="",
    country="US", postal="00123-0045", base="98765", ext="123",
):
    row = [""] * len(HEADER)
    row[0] = str(key)
    for index, value in zip(SCORE_INDEXES, ("M", received, awareness, country, postal, base, ext)):
        row[index] = value
    for index, value in zip(SCOPE_INDEXES, (kind, phase, summary, exemption, number)):
        row[index] = value
    return row


def master_file(path, rows):
    with ZipFile(path, "w") as archive:
        archive.writestr("master.txt", "".join("|".join(row) + "\n" for row in (HEADER, *rows)).encode("latin1"))


class MaudeQualityTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.archive = self.directory / "archive.zip"
        self.current = self.directory / "current.zip"
        self.output = self.directory / "governed"

    def test_metric_denominators(self):
        counts = Counts()
        records = [
            ("01/01/2026", "01/01/2026", "00123-0045", "00123", "0045"),
            ("01/01/2026", "02/01/2026", "00123-0045", "00123", "1234"),
            ("01/31/2026", "01/01/2026", "", "", "invalid"),
            ("unparseable", "01/01/2026", "", "", ""),
            ("", "01/01/2026", "", "", ""),
        ]
        for awareness, received, postal, base, ext in records:
            count_report(counts, awareness, parse_date(received), parse_date(awareness), postal, base, ext)
        scores = counts.export()
        self.assertEqual(scores["C"], metric(4, 5))
        self.assertEqual(scores["T_30"], metric(1, 2))
        self.assertEqual(scores["S"], metric(1, 2))
        self.assertEqual(scores["V"], metric(2, 3))
        self.assertEqual(scores["counts"]["negative_delay"], 1)
        self.assertIsNone(Counts().export()["T_30"]["ratio"])

    def test_date_bounds_and_invalid(self):
        date = parse_date("01/01/2026")
        for received in ("01/01/2026", "01/31/2026"):
            self.assertEqual(date_status(parse_date(received), date, "01/01/2026"), TIMELY)
        self.assertEqual(date_status(parse_date("02/01/2026"), date, "01/01/2026"), "over_30_days")
        self.assertEqual(date_status(parse_date("12/31/2025"), date, "01/01/2026"), "negative_delay")
        self.assertIsNone(parse_date("02/30/2026"))

    def test_export_filters_repairs_and_preserves_original_dates(self):
        multiline = report(1)
        multiline[18] = "Bâtiment A\nétage 2"
        master_file(self.archive, [
            multiline,
            report(2, received="02/01/2026"),
            report(3, awareness=""),
            report(4, summary="Y"),
            report(5, phase="S"),
            report(6, kind="Five-Day"),
            report(7, exemption="1234"),
            report(8, number="2"),
        ])
        master_file(self.current, [
            report(9, kind="Initial", phase=" S, i ", country="CA"),
            report(10, postal="", ext="bad"),
        ])

        result = export_subset(self.archive, self.current, self.output)
        self.assertEqual(result["retention"], metric(3, 10))
        self.assertEqual(result["retained_by_report_type"], {"THIRTY-DAY": 2, "INITIAL": 1})
        self.assertEqual(result["zip_records_repaired"], 1)
        self.assertEqual(result["after"]["C"], metric(3, 3))
        self.assertEqual(result["after"]["T_30"], metric(3, 3))
        self.assertEqual(result["after"]["S"], metric(1, 1))
        self.assertEqual(result["after"]["V"], metric(1, 2))
        self.assertTrue((self.output / REPORT_NAME).is_file())
        with gzip.open(self.output / "maude_timely_archive.psv.gz", "rt", encoding="latin1", newline="") as stream:
            rows = list(csv.reader(stream, delimiter="|"))
        expected = multiline.copy()
        expected[29], expected[30] = "00123", "0045"
        self.assertEqual(rows, [HEADER, expected])

    def test_projection_counts_original_but_excludes_export(self):
        projected = report(1, country="", postal="", base="", ext="")
        projected.insert(25, "")
        raw = ("|".join(projected) + "\n").encode("latin1")
        master_file(self.archive, [projected])
        master_file(self.current, [report(2)])
        approved = {1: (hashlib.sha256(raw).hexdigest(), 1)}
        with patch("quality_governance.relational.reader.MEASUREMENT_PROJECTIONS", approved):
            result = export_subset(self.archive, self.current, self.output)
        self.assertEqual(result["retention"], metric(1, 2))
        self.assertEqual(result["partitions"]["archive"]["rows"], 0)

    def test_invalid_inputs_leave_no_partial_output(self):
        master_file(self.archive, [report(1)])
        master_file(self.current, [report(1)])
        with self.assertRaisesRegex(ValueError, "duplicate report key"):
            export_subset(self.archive, self.current, self.output)
        self.assertFalse(self.output.exists())
        with self.assertRaisesRegex(ValueError, "frozen archive"):
            export_subset(self.archive, self.current, self.output, verify_frozen=True)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
