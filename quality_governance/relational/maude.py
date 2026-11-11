"""Export and score ordinary initial MAUDE reports received within 30 days."""

import argparse
from collections import Counter
import csv
import gzip
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import BadZipFile

from .dates import TIMELY, date_status, is_ordinary_initial, parse_date
from .frozen import verify_counts, verify_source
from .postal import correct_contact_zip
from .reader import ReportKeys, SCORE_COLUMNS, iter_rows, open_master
from .scoring import Counts, count_report, metric, pooled

SCOPE_COLUMNS = (
    "MFR_REPORT_TYPE", "TYPE_OF_REPORT", "SUMMARY_REPORT", "EXEMPTION_NUMBER", "NOE_SUMMARIZED",
)
REPORT_NAME = "maude_timely_subset_scores.json"


def export_partition(path, output, partition, keys, verify_frozen):
    """Write one filtered partition and return its original and selected counts."""
    original, before, after = Counts(), Counts(), Counts()
    labels = Counter()
    zip_records_repaired = 0
    filename = f"maude_timely_{partition}.psv.gz"
    with open_master(path) as (header, source):
        score_indexes = tuple(header.index(name) for name in SCORE_COLUMNS)
        source_index, received_index, awareness_index, country_index, postal_index, base_index, ext_index = (
            score_indexes
        )
        scope_indexes = tuple(header.index(name) for name in SCOPE_COLUMNS)
        with gzip.open(output / filename, "wt", encoding="latin1", newline="", compresslevel=1) as stream:
            writer = csv.writer(stream, delimiter="|", lineterminator="\n")
            writer.writerow(header)
            for key, row, projection in iter_rows(source, header, score_indexes):
                if keys.add(key):
                    raise ValueError(f"{path}: duplicate report key {key}")
                if row[source_index].strip() != "M":
                    continue

                offset = projection or 0
                received = parse_date(row[received_index].strip())
                awareness_text = row[awareness_index + offset].strip()
                awareness = parse_date(awareness_text)
                postal = base = ext = ""
                if row[country_index + offset].strip() == "US":
                    postal = row[postal_index + offset].strip()
                    base = row[base_index + offset].strip()
                    ext = row[ext_index + offset].strip()
                count_report(original, awareness_text, received, awareness, postal, base, ext)

                if projection is not None:
                    continue
                kind, phases, summary, exemption, number = (row[index].strip() for index in scope_indexes)
                if not is_ordinary_initial(kind, phases, summary, exemption, number):
                    continue
                if date_status(received, awareness, awareness_text) != TIMELY:
                    continue

                labels[kind.upper()] += 1
                count_report(before, awareness_text, received, awareness, postal, base, ext)
                corrected = correct_contact_zip(postal, base, ext)
                if corrected is not None:
                    base, ext = corrected
                    row[base_index], row[ext_index] = corrected
                    zip_records_repaired += 1
                count_report(after, awareness_text, received, awareness, postal, base, ext)
                writer.writerow(row)

    if verify_frozen:
        verify_counts(partition, original, labels)
    return original, before, after, {
        "file": filename,
        "rows": after.records,
        "retained_by_report_type": dict(labels),
        "zip_records_repaired": zip_records_repaired,
        "before_zip_repair": before.export(),
        "after": after.export(),
    }


def export_subset(archive, current, output, *, verify_frozen=False):
    """Export the two master partitions and publish the completed directory."""
    if output.exists():
        raise ValueError(f"{output}: choose a new output directory")
    inputs = {"archive": archive, "current": current}
    if verify_frozen:
        for partition, path in inputs.items():
            verify_source(partition, path)
    keys = ReportKeys()
    with TemporaryDirectory(dir=output.parent) as temporary:
        staged = Path(temporary) / "subset"
        staged.mkdir()
        parts = {}
        source_rows = 0
        before_all, after_all = [], []
        kept_by_label = Counter()
        for partition, path in inputs.items():
            original, before, after, part = export_partition(path, staged, partition, keys, verify_frozen)
            source_rows += original.records
            before_all.append(before)
            after_all.append(after)
            kept_by_label.update(part["retained_by_report_type"])
            parts[partition] = part
        after = pooled(after_all).export()
        scope = (
            "Frozen historical and 2026 MAUDE source M, selected ordinary initial reports"
            if verify_frozen else "MAUDE source M, selected ordinary initial reports"
        )
        report = {
            "scope": scope,
            "selection": "Thirty-Day or Initial; I marker; no recorded summary/exemption signal; original date delay 0–30 calendar days",
            "date_values": "original FDA receipt and manufacturer awareness dates",
            "format": {"compression": "gzip", "delimiter": "|", "encoding": "latin1", "header": True},
            "retention": metric(after["C"]["eligible"], source_rows),
            "retained_by_report_type": dict(kept_by_label),
            "zip_records_repaired": sum(part["zip_records_repaired"] for part in parts.values()),
            "before_zip_repair_on_selected_rows": pooled(before_all).export(),
            "after": after,
            "partitions": parts,
        }
        (staged / REPORT_NAME).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        staged.rename(output)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True, help="historical FDA master ZIP")
    parser.add_argument("--current", type=Path, required=True, help="current FDA master ZIP")
    parser.add_argument("--output", type=Path, required=True, help="new directory for the data and score report")
    parser.add_argument("--verify-frozen", action="store_true", help="verify the exact inputs and experiment counts")
    args = parser.parse_args()
    try:
        report = export_subset(args.archive, args.current, args.output, verify_frozen=args.verify_frozen)
    except (BadZipFile, OSError, ValueError) as error:
        parser.exit(1, f"{parser.prog}: {error}\n")
    print(json.dumps({"retention": report["retention"], "after": report["after"]}, indent=2))


if __name__ == "__main__":
    main()
