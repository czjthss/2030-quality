"""Parse FDA dates and apply the experiment's initial-report scope."""

from datetime import date
from functools import lru_cache

TIMELY = "within_30_days"
MAX_DELAY_DAYS = 30


@lru_cache(maxsize=200_000)
def parse_date(value):
    """Parse a date recorded as month/day/year, or return None."""
    try:
        month, day, year = value.split("/")
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def is_ordinary_initial(kind, phases, summary, exemption, number):
    if kind.upper() not in ("THIRTY-DAY", "INITIAL"):
        return False
    if summary.upper() not in ("", "N") or exemption or (number.isdecimal() and int(number) > 1):
        return False
    return any(value.strip().upper() == "I" for value in phases.split(","))


def recorded_delay(received, awareness):
    """Return calendar days from manufacturer awareness to FDA receipt."""
    if received is None or awareness is None:
        return None
    return (received - awareness).days


def date_status(received, awareness, awareness_text):
    if received is None:
        return "fda_receipt_missing_or_unparseable"
    if awareness is None:
        return "manufacturer_awareness_unparseable" if awareness_text else "manufacturer_awareness_missing"
    delay = recorded_delay(received, awareness)
    if delay < 0:
        return "negative_delay"
    return TIMELY if delay <= MAX_DELAY_DAYS else "over_30_days"
