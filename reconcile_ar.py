"""Reconcile tenant receipts against the rent roll.

Expected side:  rent_roll.csv  -- what each occupied unit owes for the period
Received side:  ar_receipts.csv -- what tenants actually paid

Statuses assigned to each expected charge:
    paid_in_full           - received matches the amount due within tolerance
    partial_payment        - received falls short beyond tolerance
    non_payment            - rent is due and no receipt was recorded
    overpayment            - received exceeds the amount due beyond tolerance
    rent_escalation_missed - base rent paid but CAM/other charges not collected
    vacant_unit_payment    - a receipt posted against a vacant unit

Receipts that match no unit on the rent roll are reported separately as
unmatched_receipt rows.

Usage:
    python reconcile_ar.py
    python reconcile_ar.py --rent-roll R.csv --receipts A.csv --out ar_results.csv
"""

import argparse
import os
import sys
from datetime import date

from recon_common import (
    DataError, as_int, load_rows, money, money_at, write_results,
)

DEFAULT_DIR = os.path.dirname(os.path.abspath(__file__))
RENT_TOLERANCE = 0.02
GRACE_DAYS = 5

AR_RESULT_FIELDS = [
    "row_type", "property_id", "unit", "tenant", "lease_id", "receipt_id",
    "period", "amount_due", "amount_received", "variance", "days_late",
    "occupancy_status", "status", "detail",
]


def grace_deadline(period):
    """Day of the month after which a payment counts as late."""
    return date(int(period[:4]), int(period[5:7]), GRACE_DAYS)


def reconcile_ar(rent_roll, receipts, period, tolerance=RENT_TOLERANCE):
    """Return one result row per expected charge, plus one per unmatched receipt."""
    # Index receipts by (property, unit) -- a unit can receive several payments
    # in a period, so accumulate rather than overwrite.
    receipts_by_unit = {}
    for index, receipt in enumerate(receipts, start=1):
        key = (
            receipt.get("property_id", "").strip().upper(),
            receipt.get("unit", "").strip().upper(),
        )
        receipts_by_unit.setdefault(key, []).append((index, receipt))

    consumed = set()
    results = []
    label_period = period

    for roll_row in rent_roll:
        prop = roll_row.get("property_id", "").strip().upper()
        unit = roll_row.get("unit", "").strip().upper()
        lease_id = roll_row.get("lease_id", "").strip()
        tenant = roll_row.get("tenant", "")
        occupancy = roll_row.get("occupancy_status", "").strip().lower()
        label = "%s %s" % (prop or "?", unit or "?")

        due = money_at(
            roll_row.get("total_monthly_due") or 0, "total_monthly_due", label
        )
        base_rent = money_at(
            roll_row.get("monthly_rent") or 0, "monthly_rent", label
        )
        cam = money_at(roll_row.get("cam_monthly") or 0, "cam_monthly", label)

        matches = receipts_by_unit.get((prop, unit), [])
        received = 0.0
        received_ids = []
        latest = None
        for index, receipt in matches:
            received += money_at(
                receipt.get("amount_received") or 0,
                "amount_received",
                receipt.get("receipt_id") or "row %d" % index,
            )
            received_ids.append(receipt.get("receipt_id", ""))
            raw_date = receipt.get("date_received", "")
            if raw_date:
                try:
                    parsed = date.fromisoformat(raw_date)
                    if latest is None or parsed > latest:
                        latest = parsed
                except ValueError:
                    pass
        received = money(received)
        consumed.update((prop, unit) for _, _ in matches)

        # Vacant units should not be billed; a payment against one is an exception.
        if occupancy == "vacant":
            status = "vacant_unit_payment" if received > 0 else "vacant_no_charge"
            detail = (
                "receipt posted against vacant unit"
                if received > 0 else "unit vacant, no charge expected"
            )
            results.append(_row(
                "rent_roll", prop, unit, tenant, lease_id,
                ", ".join(received_ids), label_period, 0.0, received,
                money(received), None, occupancy, status, detail,
            ))
            continue

        days_late = None
        if latest is not None and received > 0:
            delay = (latest - grace_deadline(label_period)).days
            days_late = delay if delay > 0 else 0

        if received == 0:
            status = "non_payment"
            detail = "rent due %.2f, no receipt recorded" % due
        else:
            variance = money(received - due)
            # Escalation is checked before tolerance: CAM is small relative to
            # rent, so a tenant paying base rent only would otherwise fall
            # inside the tolerance band and read as paid in full.
            paid_base_only = (
                cam > 0 and due > base_rent and abs(received - base_rent) <= 1.0
            )
            if paid_base_only:
                status = "rent_escalation_missed"
                detail = "paid base rent only; CAM %.2f not collected" % cam
            elif due > 0 and abs(variance) / due <= tolerance:
                status = "paid_in_full"
                detail = ""
            elif variance < 0:
                status = "partial_payment"
                detail = "short pay of %.2f" % money(-variance)
            else:
                status = "overpayment"
                detail = "overpayment of %.2f" % variance

        results.append(_row(
            "rent_roll", prop, unit, tenant, lease_id,
            ", ".join(received_ids), label_period, due, received,
            money(received - due), days_late, occupancy, status, detail,
        ))

    # Anything left over references a unit the rent roll does not contain.
    for (prop, unit), entries in receipts_by_unit.items():
        if (prop, unit) in consumed:
            continue
        for index, receipt in entries:
            amount = money_at(
                receipt.get("amount_received") or 0,
                "amount_received",
                receipt.get("receipt_id") or "row %d" % index,
            )
            results.append(_row(
                "unmatched_receipt", prop, unit, receipt.get("tenant", ""), "",
                receipt.get("receipt_id", ""), receipt.get("period", label_period),
                0.0, amount, money(amount), None, "", "unmatched_receipt",
                "receipt references a unit not on the rent roll",
            ))

    return results


def _row(row_type, prop, unit, tenant, lease_id, receipt_id, period,
         due, received, variance, days_late, occupancy, status, detail):
    return {
        "row_type": row_type,
        "property_id": prop,
        "unit": unit,
        "tenant": tenant,
        "lease_id": lease_id,
        "receipt_id": receipt_id,
        "period": period,
        "amount_due": "%.2f" % due,
        "amount_received": "%.2f" % received,
        "variance": "%.2f" % variance,
        "days_late": "" if days_late is None else str(days_late),
        "occupancy_status": occupancy,
        "status": status,
        "detail": detail,
    }


def summarize(results):
    counts = {}
    billed = collected = shortfall = 0.0
    for row in results:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        due = float(row["amount_due"])
        got = float(row["amount_received"])
        if row["status"] == "unmatched_receipt":
            continue
        billed += due
        collected += got
        shortfall += max(0.0, due - got)

    print("")
    print("AR reconciliation summary")
    print("-" * 46)
    print("expected charges       %d" % len(results))
    for status in sorted(counts):
        print("%-22s %d" % (status, counts[status]))
    print("-" * 46)
    print("billed                 %.2f" % money(billed))
    print("collected              %.2f" % money(collected))
    print("outstanding shortfall  %.2f" % money(shortfall))
    print("")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rent-roll",
                        default=os.path.join(DEFAULT_DIR, "data", "rent_roll.csv"))
    parser.add_argument("--receipts",
                        default=os.path.join(DEFAULT_DIR, "data", "ar_receipts.csv"))
    parser.add_argument("--period", default="2026-03")
    parser.add_argument("--out", default=os.path.join(DEFAULT_DIR, "ar_results.csv"))
    parser.add_argument("--tolerance", type=float, default=RENT_TOLERANCE)
    args = parser.parse_args(argv)

    rent_roll = load_rows(args.rent_roll)
    receipts = load_rows(args.receipts)

    try:
        results = reconcile_ar(rent_roll, receipts, args.period, args.tolerance)
    except DataError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    write_results(args.out, results, AR_RESULT_FIELDS)
    print("wrote %s (%d rows)" % (args.out, len(results)))
    summarize(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
