"""Reconcile vendor invoices against approved work orders (AP, real estate).

Expected side:  ap_work_orders.csv -- approved spend per property
Received side:  ap_invoices.csv    -- what vendors billed

Statuses assigned to each invoice:
    matched            - work order found, billed amount within tolerance,
                         vendor resolves to the approved vendor
    over_budget        - billed above the approved amount beyond tolerance
    amount_variance    - billed below the approved amount beyond tolerance
    vendor_mismatch    - work order found but billed by a different entity
    no_work_order      - no usable work order reference, or not in the file
    duplicate_invoice  - this invoice number was already processed

Usage:
    python reconcile.py
    python reconcile.py --work-orders W.csv --invoices I.csv --out ap_results.csv
"""

import argparse
import os
import sys

from recon_common import (
    DataError, load_rows, money, money_at, normalize_vendor, write_results,
)

DEFAULT_DIR = os.path.dirname(os.path.abspath(__file__))
AMOUNT_TOLERANCE = 0.05

AP_RESULT_FIELDS = [
    "invoice_row", "invoice_number", "property_id", "work_order_id", "vendor",
    "gl_account", "expense_category", "invoice_amount", "approved_amount",
    "variance", "variance_pct", "status", "detail",
]


def reconcile_ap(invoices, work_orders, tolerance=AMOUNT_TOLERANCE):
    """Return one result dict per invoice, in input order."""
    wo_by_id = {}
    for wo in work_orders:
        key = wo["work_order_id"].strip().upper()
        if key:
            wo_by_id[key] = wo

    first_seen = {}
    results = []

    for row_number, invoice in enumerate(invoices, start=1):
        inv_no = invoice["invoice_number"].strip().upper()
        wo_no = invoice.get("work_order_id", "").strip().upper()
        label = invoice.get("invoice_number") or "row %d" % row_number

        status = "matched"
        details = []
        approved = None

        first_row = first_seen.setdefault(inv_no, row_number)

        if first_row != row_number:
            status = "duplicate_invoice"
            details.append(
                "invoice %s already processed on row %d" % (inv_no, first_row)
            )
        elif not wo_no or wo_no not in wo_by_id:
            status = "no_work_order"
            details.append(
                "work order %s not found in approved work orders"
                % (wo_no or "(blank)")
            )
        else:
            wo = wo_by_id[wo_no]
            approved = money_at(wo["approved_amount"], "approved_amount", wo_no)
            billed = money_at(invoice["amount"], "amount", label)

            issues = []
            if approved > 0:
                drift = (billed - approved) / approved
                if drift > tolerance:
                    issues.append((
                        "over_budget",
                        "billed %.2f against %.2f approved (+%.1f%%)"
                        % (billed, approved, drift * 100),
                    ))
                elif -drift > tolerance:
                    issues.append((
                        "amount_variance",
                        "billed %.2f against %.2f approved (%.1f%%)"
                        % (billed, approved, drift * 100),
                    ))
            if normalize_vendor(invoice["vendor"]) != normalize_vendor(wo["vendor"]):
                issues.append((
                    "vendor_mismatch",
                    "billed by %r against work order issued to %r"
                    % (invoice["vendor"], wo["vendor"]),
                ))

            if issues:
                order = {"over_budget": 0, "amount_variance": 1, "vendor_mismatch": 2}
                issues.sort(key=lambda pair: order[pair[0]])
                status = issues[0][0]
                details.extend(detail for _, detail in issues)

        billed_total = money_at(invoice["amount"], "amount", label)
        variance = 0.0 if approved is None else money(billed_total - approved)
        variance_pct = "" if not approved else "%+.1f%%" % (100.0 * variance / approved)

        results.append({
            "invoice_row": row_number,
            "invoice_number": invoice["invoice_number"],
            "property_id": invoice.get("property_id", ""),
            "work_order_id": invoice.get("work_order_id", ""),
            "vendor": invoice["vendor"],
            "gl_account": invoice.get("gl_account", ""),
            "expense_category": invoice.get("expense_category", ""),
            "invoice_amount": "%.2f" % billed_total,
            "approved_amount": "" if approved is None else "%.2f" % approved,
            "variance": "%.2f" % variance,
            "variance_pct": variance_pct,
            "status": status,
            "detail": "; ".join(details),
        })

    return results


def summarize(results):
    counts = {}
    billed = approved_total = 0.0
    exposure = 0.0

    for row in results:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        billed += abs(float(row["invoice_amount"]))
        if row["approved_amount"]:
            approved_total += float(row["approved_amount"])
        if row["status"] != "matched":
            exposure += abs(float(row["variance"]))

    print("")
    print("AP reconciliation summary")
    print("-" * 46)
    print("invoices processed     %d" % len(results))
    print("matched                %d" % counts.get("matched", 0))
    for status in sorted(counts):
        if status == "matched":
            continue
        print("%-22s %d" % (status, counts[status]))
    print("-" * 46)
    print("total billed           %.2f" % money(billed))
    print("total approved         %.2f" % money(approved_total))
    print("variance exposure      %.2f" % money(exposure))
    print("")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--work-orders",
        default=os.path.join(DEFAULT_DIR, "data", "ap_work_orders.csv"),
    )
    parser.add_argument(
        "--invoices",
        default=os.path.join(DEFAULT_DIR, "data", "ap_invoices.csv"),
    )
    parser.add_argument("--out", default=os.path.join(DEFAULT_DIR, "ap_results.csv"))
    parser.add_argument(
        "--tolerance", type=float, default=AMOUNT_TOLERANCE,
        help="fractional amount drift treated as acceptable (default 0.05)",
    )
    args = parser.parse_args(argv)

    invoices = load_rows(args.invoices)
    work_orders = load_rows(args.work_orders)

    try:
        results = reconcile_ap(invoices, work_orders, args.tolerance)
    except DataError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    write_results(args.out, results, AP_RESULT_FIELDS)
    print("wrote %s (%d rows)" % (args.out, len(results)))
    summarize(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
