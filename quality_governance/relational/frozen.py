"""Verification metadata for the two base master files used in the experiment."""

from hashlib import file_digest

from .scoring import Counts

BASELINES = {
    "archive": {
        "sha256": "f29f23e21fcee23937abbe01600484b45c1c4285c91e11cf618a96159bbf11a5",
        "manufacturer_counts": Counts(
            records=22_451_158,
            manufacturer_date_present=19_302_919,
            delay_eligible=18_832_741,
            within_30_days=16_639_844,
            negative_delay=470_178,
            consistency_eligible=1_721_099,
            consistency_pass=1_721_099,
            validity_eligible=1_721_189,
            validity_pass=1_721_100,
        ),
        "retained_by_report_type": {"THIRTY-DAY": 10_851_329, "INITIAL": 2_360_089},
    },
    "current": {
        "sha256": "2ac4c5a0a6f4922901f6461a2222f94b5c5f2d9dd7162d3920cd736a9efc44df",
        "manufacturer_counts": Counts(
            records=3_041_705,
            manufacturer_date_present=2_610_918,
            delay_eligible=2_610_538,
            within_30_days=2_106_826,
            negative_delay=380,
            consistency_eligible=658_916,
            consistency_pass=244_677,
            validity_eligible=658_984,
            validity_pass=244_677,
            consistency_and_validity_fail=414_239,
        ),
        "retained_by_report_type": {"THIRTY-DAY": 1_495_875, "INITIAL": 312_027},
    },
}


def verify_source(partition, path):
    with path.open("rb") as stream:
        actual = file_digest(stream, "sha256").hexdigest()
    if actual != BASELINES[partition]["sha256"]:
        raise ValueError(f"{path}: input does not match the frozen {partition} MAUDE file")


def verify_counts(partition, original, retained):
    expected = BASELINES[partition]
    if original != expected["manufacturer_counts"] or retained != expected["retained_by_report_type"]:
        raise ValueError(f"{partition}: report counts disagree with the frozen MAUDE baseline")
