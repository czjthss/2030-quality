"""Read FDA base master ZIPs without losing embedded address newlines."""

from contextlib import contextmanager
import hashlib
from zipfile import ZipFile

SCORE_COLUMNS = (
    "REPORT_SOURCE_CODE", "DATE_RECEIVED", "DATE_MANUFACTURER_RECEIVED",
    "MANUFACTURER_CONTACT_COUNTRY", "MANUFACTURER_CONTACT_POSTAL",
    "MANUFACTURER_CONTACT_ZIP_CODE", "MANUFACTURER_CONTACT_ZIP_EXT",
)

# These exact archive records have an extra delimiter and an independently
# verified offset for scoring. Their original layout is unsuitable for export.
MEASUREMENT_PROJECTIONS = {
    20207043: ("26858bada8b0af819cff4512fe4e7ae3b67f0633e6e435f17c27e8468c8d775e", 0),
    16617882: ("c96971d29c38db0a0cedf002ce80ef81b6db3c60e87d959aaa03f6719b0faf15", 1),
    14922135: ("cc3ff0a17f3177a64ce16296274cefb654aa32f428488202af5c68f073bfc572", 1),
}


class ReportKeys:
    """Track duplicate report keys using a bitset and an overflow set."""

    def __init__(self):
        self.bits = bytearray(1 << 23)
        self.overflow = set()

    def add(self, key):
        offset, bit = divmod(key, 8)
        if offset >= len(self.bits):
            duplicate = key in self.overflow
            self.overflow.add(key)
            return duplicate
        mask = 1 << bit
        duplicate = bool(self.bits[offset] & mask)
        self.bits[offset] |= mask
        return duplicate


def split_fields(raw):
    return raw.decode("latin1").rstrip("\r\n").split("|")


@contextmanager
def open_master(path):
    with ZipFile(path) as archive:
        members = [entry for entry in archive.infolist() if entry.filename.lower().endswith(".txt")]
        if len(members) != 1:
            raise ValueError(f"{path}: expected one master TXT file")
        with archive.open(members[0]) as stream:
            header = split_fields(stream.readline())
            if len(header) != 86 or len(set(header)) != 86 or header[0] != "MDR_REPORT_KEY":
                raise ValueError(f"{path}: unexpected FDA base master header")
            yield header, stream


def iter_rows(stream, header, score_indexes):
    """Yield the key, row, and optional archive offset for the scoring fields."""
    physical_line = 1  # The master header occupies physical line one.
    width = len(header)
    for raw in stream:
        physical_line += 1
        row = split_fields(raw)
        if not row[0].isascii() or not row[0].isdigit():
            raise ValueError(f"Unexpected record start at physical line {physical_line}")
        while len(row) < width:
            tail = next(stream, b"")
            if not tail:
                raise ValueError(f"Incomplete record at physical line {physical_line}")
            physical_line += 1
            tail_fields = split_fields(tail)
            if len(tail_fields) == width and tail_fields[0].isdigit():
                raise ValueError(f"Incomplete row before physical line {physical_line}")
            raw += tail
            row = split_fields(raw)

        key = int(row[0])
        offset = None
        if len(row) != width:
            projection = MEASUREMENT_PROJECTIONS.get(key)
            if len(row) != width + 1 or not projection or hashlib.sha256(raw).hexdigest() != projection[0]:
                raise ValueError(f"Unexpected field count at physical line {physical_line}: {len(row)}")
            if score_indexes != (3, 7, 49, 31, 32, 29, 30) or any(
                row[31 + shift].strip() == "US" and row[30 + shift].strip() for shift in (0, 1)
            ):
                raise ValueError(f"Cannot apply archive projection at physical line {physical_line}")
            offset = projection[1]
        yield key, row, offset
