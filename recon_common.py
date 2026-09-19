"""Shared helpers for both reconciliation engines (AR and AP).

Kept separate so the rent-roll and work-order engines agree on rounding, vendor
normalisation and CSV handling instead of drifting apart.
"""

import csv
import os


class DataError(ValueError):
    """Raised when input data is unusable.

    Kept distinct from SystemExit so the web layer can turn it into a 400
    response; the CLI translates it into a clean exit instead.
    """


# Suffixes that do not change which legal entity is billing us.
VENDOR_SUFFIXES = {
    "INC", "LLC", "LLP", "LTD", "CORP", "CO", "GMBH", "PLC", "SA", "NV", "BV",
}


def money(value):
    """Round to cents, absorbing binary float representation error."""
    return round(float(value) + 1e-9, 2)


def normalize_vendor(name):
    """Reduce a vendor name to a comparable key.

    Vendors bill under casing, spacing and legal-suffix variants of the same
    name ('Acme Property Services' / 'ACME PROPERTY SERVICES' / 'Acme Property
    Services Inc.'). Folding those away stops a real match being reported as a
    vendor mismatch.
    """
    if not name:
        return ""
    cleaned = name.replace(",", " ").replace(".", " ")
    tokens = [t.upper() for t in cleaned.split()]
    while tokens and tokens[-1] in VENDOR_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def load_rows(path):
    """Read a CSV into dicts with whitespace-stripped string values."""
    if not os.path.exists(path):
        raise SystemExit("input file not found: %s" % path)
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return [
            {k: (v or "").strip() for k, v in row.items() if k is not None}
            for row in csv.DictReader(handle)
        ]


def as_int(value, field, row_label):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        raise DataError("%s: %s is not a number: %r" % (row_label, field, value))


def as_float(value, field, row_label):
    try:
        return float(value)
    except (TypeError, ValueError):
        raise DataError("%s: %s is not a number: %r" % (row_label, field, value))


def money_at(value, field, row_label):
    """Parse a required currency string, raising DataError when unusable."""
    return money(as_float(value, field, row_label))


def write_results(path, results, fields):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(results)
