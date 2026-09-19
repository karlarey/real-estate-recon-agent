"""Generate synthetic real-estate data for the reconciliation agent.

Three properties: one showcase (full defect coverage) plus two smaller siblings
so the portfolio rollup aggregates real rows rather than repeating one.

Writes into this directory:
    properties.csv        - the portfolio
    rent_roll.csv         - what each tenant should pay (expected side, AR)
    ar_receipts.csv       - what tenants actually paid (received side, AR)
    ap_work_orders.csv    - approved vendor spend (expected side, AP)
    ap_invoices.csv       - vendor invoices (received side, AP)
    ground_truth_ar.csv   - correct AR status per receipt/lease
    ground_truth_ap.csv   - correct AP status per invoice

Deterministic: seeded, so every run produces identical data.
Run:  python generate_data.py
"""

import csv
import os
import random
from datetime import date, timedelta

random.seed(42)

OUT_DIR = os.path.dirname(os.path.abspath(__file__))

# GL chart of accounts, real-estate flavoured
GL_CHART = {
    "1100": ("Cash",                    "Asset"),
    "1200": ("Tenant Receivables",      "Asset"),
    "2000": ("Accounts Payable",        "Liability"),
    "2999": ("AP Reserve — Held",       "Liability"),
    "4100": ("Rental Income",           "Revenue"),
    "4200": ("CAM & Other Income",      "Revenue"),
    "5100": ("Repairs & Maintenance",   "Expense"),
    "5200": ("Utilities",               "Expense"),
    "5300": ("Property Management Fee", "Expense"),
    "5400": ("Property Tax & Insurance","Expense"),
}

# AP expense categories and the GL account each codes to
AP_CATEGORIES = {
    "Repairs":       "5100",
    "Utilities":     "5200",
    "Management":    "5300",
    "TaxInsurance":  "5400",
}

AP_VENDORS = [
    "Rapid Response Plumbing",
    "Bright Spark Electric",
    "GreenLeaf Landscaping",
    "Summit HVAC Services",
    "Citywide Water Utility",
    "Metro Power & Light",
    "Apex Property Management",
    "Guardian Insurance Group",
    "Precision Roofing Co",
    "Clearview Window Cleaning",
]

AP_WORK_ITEMS = {
    "Repairs": [
        ("Replace water heater", 380.00, 1400.00),
        ("Repair unit HVAC", 250.00, 950.00),
        ("Patch drywall and paint", 120.00, 600.00),
        ("Fix leaking faucet", 90.00, 340.00),
        ("Roof shingle repair", 400.00, 2200.00),
        ("Lock replacement", 75.00, 280.00),
    ],
    "Utilities": [
        ("Water service monthly", 600.00, 1800.00),
        ("Common area electric", 450.00, 1500.00),
        ("Gas service monthly", 300.00, 1100.00),
    ],
    "Management": [
        ("Property management fee", 1500.00, 4200.00),
        ("Leasing commission", 500.00, 1600.00),
    ],
    "TaxInsurance": [
        ("Property insurance premium", 2200.00, 6800.00),
        ("Quarterly property tax", 3000.00, 9500.00),
    ],
}

# ----------------------------------------------------------------------
# Portfolio
# ----------------------------------------------------------------------
PROPERTIES = [
    {"property_id": "PROP-01", "name": "Maple Ridge Apartments",
     "address": "1420 Maple Ridge Dr", "city": "Columbus", "state": "OH",
     "units": 24, "property_type": "Multifamily", "showcase": True},
    {"property_id": "PROP-02", "name": "Cedar Point Townhomes",
     "address": "88 Cedar Point Ln", "city": "Dublin", "state": "OH",
     "units": 8, "property_type": "Townhome", "showcase": False},
    {"property_id": "PROP-03", "name": "Elm Street Lofts",
     "address": "310 Elm St", "city": "Westerville", "state": "OH",
     "units": 6, "property_type": "Loft", "showcase": False},
]

FIRST_NAMES = [
    "Jordan", "Riley", "Casey", "Morgan", "Avery", "Quinn", "Rowan", "Sage",
    "Emerson", "Finley", "Harper", "Indigo", "Jamie", "Kendall", "Logan",
    "Marley", "Nova", "Oakley", "Parker", "Reese", "Skyler", "Tatum",
]
LAST_NAMES = [
    "Alvarez", "Bennett", "Chen", "Das", "Ellis", "Fischer", "Grant",
    "Hayes", "Ibrahim", "Jensen", "Kowalski", "Lopez", "Mitchell", "Nguyen",
    "Owens", "Patel", "Quinn", "Reyes", "Sullivan", "Tran", "Vasquez", "Wong",
]

PERIOD = "2026-03"
RENT_TOLERANCE = 0.02       # 2% under = still "paid in full"
GRACE_DAYS = 5
OPEX_RATIO = 0.38           # operating expense as a share of rent billed


def money(x):
    """Round to cents, absorbing float representation error."""
    return round(x + 1e-9, 2)


def tenant_name():
    while True:
        name = "%s %s" % (random.choice(FIRST_NAMES), random.choice(LAST_NAMES))
        # keep the demo cast from feeling cloned
        if random.random() < 0.85:
            return name


def make_rent_roll():
    """One row per unit. Vacant units carry no rent due."""
    roll = []
    lease_no = 0
    for prop in PROPERTIES:
        for unit_no in range(1, prop["units"] + 1):
            lease_no += 1
            # Readable unit numbers: "01-101" = property 01, building 1, unit 01.
            unit = "%s-%d%02d" % (prop["property_id"][-2:], random.randint(1, 3), unit_no)
            base_rent = money(random.uniform(950.0, 2400.0))
            cam = money(random.uniform(45.0, 175.0))
            other = money(random.choice([0.0, 0.0, 0.0, 35.0, 60.0]))
            total = money(base_rent + cam + other)

            # ~8% vacancy so occupancy is not a boring 100%
            vacant = random.random() < 0.08
            start = date(2024, 1, 1) + timedelta(days=random.randint(0, 700))
            end = start + timedelta(days=365)

            roll.append({
                "lease_id": "LSE-%d" % (5000 + lease_no),
                "property_id": prop["property_id"],
                "unit": unit,
                "tenant": "" if vacant else tenant_name(),
                "lease_start": start.isoformat(),
                "lease_end": end.isoformat(),
                "monthly_rent": 0.0 if vacant else base_rent,
                "cam_monthly": 0.0 if vacant else cam,
                "other_charges": 0.0 if vacant else other,
                "total_monthly_due": 0.0 if vacant else total,
                "occupancy_status": "vacant" if vacant else "occupied",
            })
    return roll


def make_receipts(roll):
    """Pair each occupied lease with a payment scenario; return receipts + truth.

    Defect counts are derived from the number of occupied leases so the totals
    always agree -- a hardcoded count would silently truncate under zip().
    """
    occupied = [r for r in roll if r["occupancy_status"] == "occupied"]

    # Each occupied lease gets exactly one payment scenario. Stray receipts that
    # reference unknown units are appended separately -- folding them into this
    # mix would leave the lease they were derived from unexplained on the roll.
    defects = {
        "partial_payment":   max(1, int(len(occupied) * 0.12)),
        "non_payment":       max(1, int(len(occupied) * 0.08)),
        "overpayment":       max(1, int(len(occupied) * 0.05)),
        "escalation_missed": max(1, int(len(occupied) * 0.06)),
    }
    paid = len(occupied) - sum(defects.values())
    assert paid > 0, "not enough occupied leases for the defect mix"

    scenarios = ["paid_in_full"] * paid
    for scenario, count in defects.items():
        scenarios.extend([scenario] * count)
    assert len(scenarios) == len(occupied), "scenario count must match occupied leases"
    random.shuffle(scenarios)

    receipts = []
    truth = []
    receipt_no = 7000

    for lease, scenario in zip(occupied, scenarios):
        receipt_no += 1
        due = lease["total_monthly_due"]
        amount = due
        status = "paid_in_full"
        note = ""

        if scenario == "partial_payment":
            amount = money(due * random.uniform(0.45, 0.92))
            status = "partial_payment"
            note = "short pay of %.2f" % money(due - amount)
        elif scenario == "non_payment":
            # No receipt row at all -- the rent roll expects money that never came.
            truth.append({
                "lease_id": lease["lease_id"],
                "receipt_id": "",
                "unit": lease["unit"],
                "tenant": lease["tenant"],
                "amount_due": "%.2f" % due,
                "amount_received": "0.00",
                "expected_status": "non_payment",
                "note": "no receipt recorded for period",
            })
            continue
        elif scenario == "overpayment":
            amount = money(due * random.uniform(1.05, 1.35))
            status = "overpayment"
            note = "overpayment of %.2f" % money(amount - due)
        elif scenario == "escalation_missed":
            # Paid the pre-escalation base rent, ignoring the CAM charge.
            # CAM is small relative to rent, so a tolerance check alone would
            # wave this through -- the reconciler checks it before tolerance.
            amount = money(lease["monthly_rent"])
            status = "rent_escalation_missed"
            note = "paid base rent only; CAM %.2f not collected" % lease["cam_monthly"]

        received_on = date(2026, 3, random.randint(1, 12))
        delay = (received_on - date(2026, 3, GRACE_DAYS)).days
        days_late = delay if delay > 0 else 0

        receipts.append({
            "receipt_id": "RCP-%d" % receipt_no,
            "property_id": lease["property_id"],
            "unit": lease["unit"],
            "tenant": lease["tenant"],
            "period": PERIOD,
            "amount_received": "%.2f" % money(amount),
            "date_received": received_on.isoformat(),
            "method": random.choice(["ACH", "Check", "Portal", "Wire"]),
        })
        truth.append({
            "lease_id": lease["lease_id"],
            "receipt_id": "RCP-%d" % receipt_no,
            "unit": lease["unit"],
            "tenant": lease["tenant"],
            "amount_due": "%.2f" % due,
            "amount_received": "%.2f" % money(amount),
            "expected_status": status,
            "note": note,
        })

    # Stray receipts: money posted to units that are not on the rent roll.
    for _ in range(max(1, int(len(occupied) * 0.05))):
        receipt_no += 1
        receipt_id = "RCP-%d" % receipt_no
        prop = random.choice(PROPERTIES)["property_id"]
        unit = "ZZ-%d" % random.randint(100, 999)
        amount = money(random.uniform(400.0, 2400.0))
        received_on = date(2026, 3, random.randint(1, 12))

        receipts.append({
            "receipt_id": receipt_id,
            "property_id": prop,
            "unit": unit,
            "tenant": tenant_name(),
            "period": PERIOD,
            "amount_received": "%.2f" % amount,
            "date_received": received_on.isoformat(),
            "method": random.choice(["ACH", "Check", "Portal", "Wire"]),
        })
        truth.append({
            "lease_id": "",
            "receipt_id": receipt_id,
            "unit": unit,
            "tenant": "",
            "amount_due": "0.00",
            "amount_received": "%.2f" % amount,
            "expected_status": "unmatched_receipt",
            "note": "receipt references a unit not on the rent roll",
        })

    # Vacant units carry no charge. Recording them in ground truth means a
    # regression in vacancy handling shows up as a scoring failure rather than
    # being silently skipped.
    for lease in roll:
        if lease["occupancy_status"] != "vacant":
            continue
        truth.append({
            "lease_id": lease["lease_id"],
            "receipt_id": "",
            "unit": lease["unit"],
            "tenant": "",
            "amount_due": "0.00",
            "amount_received": "0.00",
            "expected_status": "vacant_no_charge",
            "note": "unit vacant, no charge expected",
        })

    return receipts, truth


def make_work_orders(roll):
    """Approved vendor spend, AP's expected side.

    Sized against rent actually billed so operating expense lands near the
    industry norm. Drawing amounts independently of revenue produced $77k of
    spend against $50k of monthly rent, which made NOI deeply negative and the
    whole demo read as broken.
    """
    billed_by_prop = {}
    for lease in roll:
        if lease["occupancy_status"] == "occupied":
            pid = lease["property_id"]
            billed_by_prop[pid] = billed_by_prop.get(pid, 0.0) + lease["total_monthly_due"]

    orders = []
    wo_no = 3000
    for prop in PROPERTIES:
        pid = prop["property_id"]
        n = 18 if prop["showcase"] else 7
        target = billed_by_prop.get(pid, 0.0) * OPEX_RATIO

        # Draw the category mix first, then scale the whole set to hit the
        # target. Scaling keeps the relative variation between work orders
        # instead of flattening them all to the same amount.
        picks = []
        for _ in range(n):
            category = random.choice(list(AP_CATEGORIES))
            description, lo, hi = random.choice(AP_WORK_ITEMS[category])
            picks.append((category, description, random.uniform(lo, hi)))

        raw_total = sum(weight for _, _, weight in picks)
        scale = (target / raw_total) if raw_total else 0.0

        for category, description, raw in picks:
            wo_no += 1
            approved_on = date(2026, 1, 1) + timedelta(days=random.randint(0, 75))
            orders.append({
                "work_order_id": "WO-%d" % wo_no,
                "property_id": pid,
                "vendor": random.choice(AP_VENDORS),
                "gl_account": AP_CATEGORIES[category],
                "expense_category": category,
                "description": description,
                "approved_amount": money(raw * scale),
                "approved_date": approved_on.isoformat(),
            })
    return orders


def make_ap_invoices(work_orders):
    """Pair each work order with a billing scenario, then inject duplicates."""
    defects = {
        "over_budget":      max(1, int(len(work_orders) * 0.15)),
        "amount_variance":  max(1, int(len(work_orders) * 0.08)),
        "vendor_variant":   max(1, int(len(work_orders) * 0.06)),
        "no_work_order":    max(1, int(len(work_orders) * 0.08)),
    }
    clean = len(work_orders) - sum(defects.values())
    assert clean > 0, "not enough work orders for the defect mix"

    scenarios = ["matched"] * clean
    for scenario, count in defects.items():
        scenarios.extend([scenario] * count)
    assert len(scenarios) == len(work_orders), "scenario count must match work orders"
    random.shuffle(scenarios)

    invoices = []
    truth = []
    inv_no = 9000

    for wo, scenario in zip(work_orders, scenarios):
        inv_no += 1
        approved = wo["approved_amount"]
        amount = approved
        wo_id = wo["work_order_id"]
        vendor = wo["vendor"]
        status = "matched"
        note = ""

        if scenario == "over_budget":
            amount = money(approved * random.uniform(1.12, 1.45))
            status = "over_budget"
            note = "billed %.2f against %.2f approved" % (amount, approved)
        elif scenario == "amount_variance":
            amount = money(approved * random.uniform(0.80, 0.94))
            status = "amount_variance"
            note = "billed %.2f against %.2f approved" % (amount, approved)
        elif scenario == "vendor_variant":
            vendor = random.choice([
                wo["vendor"].upper(),
                wo["vendor"].lower(),
                wo["vendor"] + " Inc.",
            ])
            note = "vendor name variant, same entity"
        elif scenario == "no_work_order":
            wo_id = "WO-%d" % (8000 + inv_no) if random.random() < 0.5 else ""
            status = "no_work_order"
            note = "referenced work order not in approved list"

        inv_date = date.fromisoformat(wo["approved_date"]) + timedelta(days=random.randint(1, 25))
        invoices.append({
            "invoice_number": "INV-%d" % inv_no,
            "property_id": wo["property_id"],
            "work_order_id": wo_id,
            "vendor": vendor,
            "invoice_date": inv_date.isoformat(),
            "description": wo["description"],
            "gl_account": wo["gl_account"],
            "expense_category": wo["expense_category"],
            "amount": "%.2f" % money(amount),
        })
        truth.append({
            "invoice_number": "INV-%d" % inv_no,
            "work_order_id": wo_id,
            "expected_status": status,
            "note": note,
        })

    # Duplicate billing
    for original in random.sample(invoices, 3):
        invoices.append(dict(original))
        truth.append({
            "invoice_number": original["invoice_number"],
            "work_order_id": original["work_order_id"],
            "expected_status": "duplicate_invoice",
            "note": "invoice number already seen",
        })

    return invoices, truth


def write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print("wrote %-24s %d rows" % (os.path.basename(path), len(rows)))


def main():
    props = [{k: v for k, v in p.items() if k != "showcase"} for p in PROPERTIES]
    roll = make_rent_roll()
    receipts, truth_ar = make_receipts(roll)
    work_orders = make_work_orders(roll)
    invoices, truth_ap = make_ap_invoices(work_orders)

    # Operating expense should land in a believable band against rent billed.
    # An out-of-band ratio is the first sign the demo data has drifted into
    # nonsense (a portfolio running at 150% opex reads as broken, not as a
    # finding), so assert it here rather than discovering it in the UI.
    billed = sum(
        l["total_monthly_due"] for l in roll if l["occupancy_status"] == "occupied"
    )
    approved = sum(float(w["approved_amount"]) for w in work_orders)
    ratio = approved / billed if billed else 0.0
    assert 0.25 <= ratio <= 0.55, (
        "opex ratio %.0f%% is outside the believable 25-55%% band "
        "(approved %.2f against billed %.2f)" % (ratio * 100, approved, billed)
    )
    print("opex ratio             %.1f%% of rent billed" % (ratio * 100))

    write_csv(os.path.join(OUT_DIR, "properties.csv"), props,
              ["property_id", "name", "address", "city", "state", "units",
               "property_type"])
    write_csv(os.path.join(OUT_DIR, "rent_roll.csv"), roll,
              ["lease_id", "property_id", "unit", "tenant", "lease_start",
               "lease_end", "monthly_rent", "cam_monthly", "other_charges",
               "total_monthly_due", "occupancy_status"])
    write_csv(os.path.join(OUT_DIR, "ar_receipts.csv"), receipts,
              ["receipt_id", "property_id", "unit", "tenant", "period",
               "amount_received", "date_received", "method"])
    write_csv(os.path.join(OUT_DIR, "ap_work_orders.csv"), work_orders,
              ["work_order_id", "property_id", "vendor", "gl_account",
               "expense_category", "description", "approved_amount",
               "approved_date"])
    write_csv(os.path.join(OUT_DIR, "ap_invoices.csv"), invoices,
              ["invoice_number", "property_id", "work_order_id", "vendor",
               "invoice_date", "description", "gl_account", "expense_category",
               "amount"])
    write_csv(os.path.join(OUT_DIR, "ground_truth_ar.csv"), truth_ar,
              ["lease_id", "receipt_id", "unit", "tenant", "amount_due",
               "amount_received", "expected_status", "note"])
    write_csv(os.path.join(OUT_DIR, "ground_truth_ap.csv"), truth_ap,
              ["invoice_number", "work_order_id", "expected_status", "note"])


if __name__ == "__main__":
    main()
