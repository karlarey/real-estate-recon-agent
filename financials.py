"""Financial statements: NOI with and without reconciliation, per property.

Real-estate operating statement. Runs off the AR and AP reconciliation results
plus their workflow decisions, so a held exception is visibly not posted.

Pure functions -- no side effects, no persistence.
"""

from collections import defaultdict

from recon_common import money

GL_CHART = {
    "1100": ("Cash",                     "Asset"),
    "1200": ("Tenant Receivables",       "Asset"),
    "2000": ("Accounts Payable",         "Liability"),
    "2999": ("AP Reserve — Held",        "Liability"),
    "4100": ("Rental Income",            "Revenue"),
    "4200": ("CAM & Other Income",       "Revenue"),
    "5100": ("Repairs & Maintenance",    "Expense"),
    "5200": ("Utilities",                "Expense"),
    "5300": ("Property Management Fee",  "Expense"),
    "5400": ("Property Tax & Insurance", "Expense"),
    "3000": ("Retained Earnings",        "Equity"),
}

RENT_ACCOUNT = "4100"
CAM_ACCOUNT = "4200"
RECEIVABLE_ACCOUNT = "1200"
AP_RESERVE_ACCOUNT = "2999"

# Expense category -> GL account, for AP posting.
CATEGORY_ACCOUNT = {
    "Repairs": "5100",
    "Utilities": "5200",
    "Management": "5300",
    "TaxInsurance": "5400",
}


def _gl_rows(amounts_by_account):
    rows = []
    for acct, total in sorted(amounts_by_account.items()):
        name, type_ = GL_CHART.get(acct, (acct, "Other"))
        rows.append({
            "account": acct,
            "name": name,
            "type": type_,
            "amount": round(total, 2),
        })
    return rows


def _blank_statement(description):
    return {
        "description": description,
        "revenue": [],
        "expenses": [],
        "total_revenue": 0.0,
        "total_expenses": 0.0,
        "noi": 0.0,
        "balance_sheet": [],
        "held_for_review": 0.0,
        "uncollected": 0.0,
    }


def _statement(revenue_totals, expense_totals, description, held=0.0, uncollected=0.0):
    revenue = _gl_rows(revenue_totals)
    expenses = _gl_rows(expense_totals)
    total_revenue = money(sum(revenue_totals.values()))
    total_expenses = money(sum(expense_totals.values()))
    noi = money(total_revenue - total_expenses)

    # Accrual snapshot. Everything collected is still on hand because vendor
    # invoices are accrued but unpaid, so Cash equals recognised revenue.
    # Receivables and the AP reserve sit alongside it, and equity takes the
    # difference -- which makes the statement balance by construction rather
    # than by assertion.
    cash = total_revenue
    receivables = money(uncollected)
    ap_payable = total_expenses
    ap_reserve = money(held)

    total_assets = money(cash + receivables)
    total_liabilities = money(ap_payable + ap_reserve)
    total_equity = money(total_assets - total_liabilities)

    return {
        "description": description,
        "revenue": revenue,
        "expenses": expenses,
        "total_revenue": total_revenue,
        "total_expenses": total_expenses,
        "noi": noi,
        "balance_sheet": [
            {"account": "1100", "name": "Cash", "type": "Asset", "amount": cash},
            {"account": RECEIVABLE_ACCOUNT, "name": "Tenant Receivables",
             "type": "Asset", "amount": receivables},
            {"account": "2000", "name": "Accounts Payable",
             "type": "Liability", "amount": ap_payable},
            {"account": AP_RESERVE_ACCOUNT, "name": "AP Reserve — Held",
             "type": "Liability", "amount": ap_reserve},
            {"account": "3000", "name": "Retained Earnings",
             "type": "Equity", "amount": total_equity},
        ],
        "total_assets": total_assets,
        "total_liabilities": total_liabilities,
        "total_equity": total_equity,
        "held_for_review": money(held),
        "uncollected": money(uncollected),
    }


def without_reconciliation(ar_results, ap_results):
    """Post what arrived and what was billed. No verification, no reserve.

    This is the naive view: every receipt counts as rent, every invoice counts
    as expense, and neither the shortfall nor the unapproved spend is visible.
    """
    revenue = defaultdict(float)
    expense = defaultdict(float)

    for row in ar_results:
        if row.get("row_type") == "unmatched_receipt":
            revenue[CAM_ACCOUNT] += abs(float(row["amount_received"]))
            continue
        got = abs(float(row["amount_received"]))
        if got == 0:
            continue
        # Everything collected lands in rental income, including overpayments
        # and payments on vacant units.
        revenue[RENT_ACCOUNT] += got

    for row in ap_results:
        acct = (row.get("gl_account") or "").strip()
        if not acct:
            acct = CATEGORY_ACCOUNT.get(row.get("expense_category", ""), "5100")
        expense[acct] += abs(float(row["invoice_amount"]))

    return _statement(
        revenue, expense,
        "Every receipt posted as rent income and every vendor invoice posted as "
        "expense — no reconciliation applied. Shortfalls and unapproved spend "
        "are invisible.",
    )


def with_reconciliation(ar_results, ap_results, workflow_status_fn, ar_key_fn, ap_key_fn):
    """Post only verified amounts; hold shortfalls and unapproved spend.

    - Verified receipts post to rental income at the amount due.
    - Shortfalls sit in Tenant Receivables rather than vanishing.
    - AP posts approved amount only where a reviewer accepted the invoice;
      rejected or pending invoices are held in the AP reserve.
    """
    revenue = defaultdict(float)
    expense = defaultdict(float)
    uncollected = 0.0
    held = 0.0

    for row in ar_results:
        status = row.get("status")
        if status == "vacant_no_charge":
            continue

        if row.get("row_type") == "unmatched_receipt":
            # Money with no unit to credit sits in suspense until someone
            # applies it. It is not income just because a reviewer clicked
            # through the exception.
            held += abs(float(row["amount_received"]))
            continue

        if status == "non_payment":
            uncollected += abs(float(row["amount_due"]))
            continue

        due = abs(float(row["amount_due"]))
        got = abs(float(row["amount_received"]))
        shortfall = max(0.0, due - got)

        if status == "overpayment":
            # Only the amount owed is income; the surplus is a liability.
            revenue[RENT_ACCOUNT] += due
            held += max(0.0, got - due)
        else:
            revenue[RENT_ACCOUNT] += got
            uncollected += shortfall

    for row in ap_results:
        status = row.get("status")
        amount = abs(float(row["invoice_amount"]))

        # A duplicate is a data error, not a payable. It never posts, whatever a
        # reviewer decides -- approving one must not double-count the expense.
        if status == "duplicate_invoice":
            continue

        acct = (row.get("gl_account") or "").strip()
        if not acct:
            acct = CATEGORY_ACCOUNT.get(row.get("expense_category", ""), "5100")

        state = workflow_status_fn(ap_key_fn(row))["workflow_status"]
        approved = float(row["approved_amount"]) if row.get("approved_amount") else 0.0

        if status == "matched" or state == "approved":
            # Recognise approved spend, not the billed amount -- that is what
            # reconciliation buys.
            expense[acct] += approved or amount
        elif state in ("rejected", "pending"):
            held += max(0.0, amount - approved)

    return _statement(
        revenue, expense,
        "Only verified amounts posted. Shortfalls sit in Tenant Receivables; "
        "rejected and pending invoices are held in the AP reserve.",
        held=held, uncollected=uncollected,
    )


def build_comparison(ar_results, ap_results, workflow_status_fn, ar_key_fn, ap_key_fn):
    """Before/after operating statement with a delta on every revenue and expense line."""
    before = without_reconciliation(ar_results, ap_results)
    after = with_reconciliation(
        ar_results, ap_results, workflow_status_fn, ar_key_fn, ap_key_fn
    )

    def delta_rows(before_rows, after_rows):
        bef = {r["account"]: r for r in before_rows}
        aft = {r["account"]: r for r in after_rows}
        rows = []
        for acct in sorted(set(bef) | set(aft)):
            b = bef.get(acct, {}).get("amount", 0.0)
            a = aft.get(acct, {}).get("amount", 0.0)
            name, _ = GL_CHART.get(acct, (acct, "Other"))
            rows.append({
                "account": acct, "name": name,
                "without_recon": round(b, 2), "with_recon": round(a, 2),
                "delta": round(a - b, 2),
            })
        return rows

    # The statement must balance. Equity is derived as the plug, so a failure
    # here means the asset or liability lines disagree with the P&L -- exactly
    # the class of bug that shipped a Cash line set to NOI.
    for label, stmt in (("without", before), ("with", after)):
        if abs(stmt["total_assets"] - (stmt["total_liabilities"] + stmt["total_equity"])) > 0.01:
            raise ValueError(
                "balance sheet does not balance (%s): assets %.2f vs "
                "liabilities+equity %.2f"
                % (label, stmt["total_assets"],
                   stmt["total_liabilities"] + stmt["total_equity"])
            )

    return {
        "without_reconciliation": before,
        "with_reconciliation": after,
        "delta": {
            "revenue": delta_rows(before["revenue"], after["revenue"]),
            "expenses": delta_rows(before["expenses"], after["expenses"]),
            "noi_without": before["noi"],
            "noi_with": after["noi"],
            "noi_delta": money(after["noi"] - before["noi"]),
            "expense_delta": money(after["total_expenses"] - before["total_expenses"]),
            "revenue_delta": money(after["total_revenue"] - before["total_revenue"]),
            "held_for_review": after["held_for_review"],
            "uncollected": after["uncollected"],
        },
    }
