"""Count eligible MAUDE manufacturer records and calculate quality scores."""

from dataclasses import asdict, dataclass, fields

from .dates import MAX_DELAY_DAYS, recorded_delay
from .postal import FULL_ZIP4, ZIP4


def metric(passed, eligible):
    return {"pass": passed, "eligible": eligible, "ratio": passed / eligible if eligible else None}


@dataclass
class Counts:
    records: int = 0
    manufacturer_date_present: int = 0
    delay_eligible: int = 0
    within_30_days: int = 0
    negative_delay: int = 0
    consistency_eligible: int = 0
    consistency_pass: int = 0
    validity_eligible: int = 0
    validity_pass: int = 0
    consistency_and_validity_fail: int = 0

    def export(self):
        """Calculate completeness, consistency, 30-day timeliness and validity."""
        return {
            "counts": asdict(self),
            "C": metric(self.manufacturer_date_present, self.records),
            "S": metric(self.consistency_pass, self.consistency_eligible),
            "T_30": metric(self.within_30_days, self.delay_eligible),
            "V": metric(self.validity_pass, self.validity_eligible),
        }


def count_report(counter, awareness_text, received, awareness, postal, zip_base, zip_ext):
    """Accumulate scoring counts for one record, with non-U.S. ZIP fields blank."""
    counter.records += 1
    # Completeness counts the presence of the recorded manufacturer date.
    counter.manufacturer_date_present += bool(awareness_text)

    # Timeliness includes only parseable dates in chronological order.
    delay = recorded_delay(received, awareness)
    if delay is not None:
        if delay >= 0:
            counter.delay_eligible += 1
            counter.within_30_days += delay <= MAX_DELAY_DAYS
        else:
            counter.negative_delay += 1

    if zip_ext:
        # Validity checks each nonempty extension for four ASCII digits.
        valid = bool(ZIP4.fullmatch(zip_ext))
        counter.validity_eligible += 1
        counter.validity_pass += valid

        # Consistency compares a complete ZIP+4 with its two split fields.
        match = FULL_ZIP4.fullmatch(postal)
        if match and zip_base:
            consistent = (zip_base, zip_ext) == match.groups()
            counter.consistency_eligible += 1
            counter.consistency_pass += consistent
            counter.consistency_and_validity_fail += not consistent and not valid


def pooled(counters):
    counters = tuple(counters)
    return Counts(**{
        field.name: sum(getattr(counter, field.name) for counter in counters)
        for field in fields(Counts)
    })
