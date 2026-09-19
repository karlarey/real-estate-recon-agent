"""Run the full pipeline: generate data, reconcile AR and AP, score, validate.

    python run_all.py

Exits non-zero if any step fails.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "data"))

import generate_data  # noqa: E402
import reconcile  # noqa: E402
import reconcile_ar  # noqa: E402
import score  # noqa: E402
import agent  # noqa: E402
from recon_common import DataError  # noqa: E402

PERIOD = "2026-03"


def check_bad_data():
    """Malformed numeric input must raise DataError, not SystemExit.

    SystemExit inside a request handler tears down the worker; DataError is what
    the API turns into a 400. Checked on both engines.
    """
    failures = 0

    bad_receipts = [{
        "receipt_id": "RCP-X", "property_id": "PROP-01", "unit": "UT-1",
        "tenant": "Test", "period": PERIOD,
        "amount_received": "not-a-number", "date_received": "2026-03-05",
        "method": "ACH",
    }]
    roll = [{
        "lease_id": "LSE-X", "property_id": "PROP-01", "unit": "UT-1",
        "tenant": "Test", "monthly_rent": "1000.00", "cam_monthly": "50.00",
        "other_charges": "0.00", "total_monthly_due": "1050.00",
        "occupancy_status": "occupied",
    }]
    try:
        reconcile_ar.reconcile_ar(roll, bad_receipts, PERIOD)
        print("FAIL: AR accepted a non-numeric amount without error")
        failures += 1
    except DataError as exc:
        print("AR non-numeric raises DataError: %s" % exc)
    except SystemExit:
        print("FAIL: AR raised SystemExit — would kill the web worker")
        failures += 1

    bad_invoices = [{
        "invoice_number": "INV-X", "property_id": "PROP-01",
        "work_order_id": "WO-1", "vendor": "Test Vendor",
        "invoice_date": "2026-02-01", "description": "Test",
        "gl_account": "5100", "expense_category": "Repairs",
        "amount": "not-a-number",
    }]
    orders = [{
        "work_order_id": "WO-1", "property_id": "PROP-01", "vendor": "Test Vendor",
        "gl_account": "5100", "expense_category": "Repairs",
        "description": "Test", "approved_amount": "500.00",
        "approved_date": "2026-01-15",
    }]
    try:
        reconcile.reconcile_ap(bad_invoices, orders)
        print("FAIL: AP accepted a non-numeric amount without error")
        failures += 1
    except DataError as exc:
        print("AP non-numeric raises DataError: %s" % exc)
    except SystemExit:
        print("FAIL: AP raised SystemExit — would kill the web worker")
        failures += 1

    return 1 if failures else 0


def check_agent():
    """The advisor's rules path must produce sane verdicts with no network.

    Non-payment and overpayment are material and must escalate; a duplicate is a
    control failure and must reject; a small variance waives to approve.
    """
    failures = 0

    cases = [
        # AR: a large non-payment escalates
        ({"side": "AR", "status": "non_payment", "amount_due": "3200.00",
          "amount_received": "0.00", "days_late": "20", "lease_id": "LSE-1",
          "unit": "01-101", "detail": "no receipt"}, "escalate"),
        # AP: a duplicate always rejects
        ({"side": "AP", "status": "duplicate_invoice", "invoice_amount": "450.00",
          "approved_amount": "", "invoice_number": "INV-1", "detail": "already seen"},
         "reject"),
        # AP: a small over-budget within 10% waives to approve
        ({"side": "AP", "status": "over_budget", "invoice_amount": "525.00",
          "approved_amount": "500.00", "invoice_number": "INV-2", "detail": "+5%"},
         "approve"),
        # AR: a small partial payment (shortfall < 10%) waives to approve
        ({"side": "AR", "status": "partial_payment", "amount_due": "1000.00",
          "amount_received": "980.00", "days_late": "1", "lease_id": "LSE-2",
          "unit": "01-102", "detail": "short pay 20"}, "approve"),
    ]

    for row, want in cases:
        verdict = agent.review(row, {}, use_llm=False)
        got = verdict["recommendation"]
        ok = got == want
        print("agent %-18s -> %-10s (want %s) %s"
              % (row["status"], got, want, "PASS" if ok else "FAIL"))
        if not ok:
            failures += 1
        if "signals" not in verdict or not verdict["signals"]:
            print("  FAIL: no signals exposed")
            failures += 1

    return 1 if failures else 0


def main():
    rc = 0

    print("=== 1/6 generate data ===")
    generate_data.main()

    print("=== 2/6 reconcile AR (rent roll vs receipts) ===")
    rc = reconcile_ar.main([]) or rc

    print("=== 3/6 reconcile AP (invoices vs work orders) ===")
    rc = reconcile.main([]) or rc

    print("=== 4/6 score both suites ===")
    rc = score.main() or rc

    print("=== 5/6 input validation ===")
    rc = check_bad_data() or rc

    print("=== 6/6 advisor rules ===")
    rc = check_agent() or rc

    print("")
    print("exit code: %d" % rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
