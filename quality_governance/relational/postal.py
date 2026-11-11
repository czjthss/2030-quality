"""Validate and repair split U.S. contact ZIP fields."""

import re

ZIP4 = re.compile(r"[0-9]{4}\Z")
FULL_ZIP4 = re.compile(r"([0-9]{5})-?([0-9]{4})\Z")


def correct_contact_zip(postal, base, ext):
    """Return corrected split fields only for a comparable, conflicting pair."""
    if not (postal and base and ext):
        return None
    match = FULL_ZIP4.fullmatch(postal)
    if match and (base, ext) != match.groups():
        return match.groups()
    return None
