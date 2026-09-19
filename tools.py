"""Read-only tools the chat agent may call.

Deliberately read-only: the assistant answers questions about reconciliation
data, it never writes, approves, or touches the filesystem. Every tool returns a
compact dict -- the rows plus a one-line summary -- so the model gets enough to
answer without blowing its context on the full result set.

Tools are pure functions of the state dict handed in, so they are testable
offline with no model and no network.

State shape:
    {
      "ar_results": [...], "ap_results": [...],
      "properties": [...], "portfolio": {...}, "financials": {...},
    }
"""

MAX_ROWS = 25


def _num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _money(value):
    return round(_num(value), 2)


def _ar_amount(row):
    """The amount that matters for an AR row: what was due, else what arrived."""
    return _money(row.get("amount_due")) or _money(row.get("amount_received"))


def _ap_amount(row):
    return _money(row.get("invoice_amount"))


CLEAN_AR = {"paid_in_full", "vacant_no_charge"}


# ----------------------------------------------------------------------
# Tool implementations
# ----------------------------------------------------------------------
def list_ar_exceptions(state, status="", property_id="", min_amount=0, limit=MAX_ROWS):
    """Rent-roll / receipt exceptions, optionally filtered."""
    rows = []
    for row in state.get("ar_results", []):
        if row.get("status") in CLEAN_AR:
            continue
        if status and row.get("status") != status:
            continue
        if property_id and row.get("property_id") != property_id:
            continue
        if _ar_amount(row) < _num(min_amount):
            continue
        rows.append({
            "unit": row.get("unit"),
            "tenant": row.get("tenant"),
            "property_id": row.get("property_id"),
            "lease_id": row.get("lease_id"),
            "status": row.get("status"),
            "amount_due": row.get("amount_due"),
            "amount_received": row.get("amount_received"),
            "variance": row.get("variance"),
            "days_late": row.get("days_late"),
            "detail": row.get("detail"),
        })

    rows.sort(key=lambda r: abs(_num(r["variance"])), reverse=True)
    total = sum(abs(_num(r["variance"])) for r in rows)
    return {
        "count": len(rows),
        "total_variance": _money(total),
        "rows": rows[:limit],
        "truncated": len(rows) > limit,
    }


def list_ap_exceptions(state, status="", property_id="", min_amount=0, limit=MAX_ROWS):
    """Vendor-invoice exceptions, optionally filtered."""
    rows = []
    for row in state.get("ap_results", []):
        if row.get("status") == "matched":
            continue
        if status and row.get("status") != status:
            continue
        if property_id and row.get("property_id") != property_id:
            continue
        if _ap_amount(row) < _num(min_amount):
            continue
        rows.append({
            "invoice_number": row.get("invoice_number"),
            "vendor": row.get("vendor"),
            "property_id": row.get("property_id"),
            "work_order_id": row.get("work_order_id"),
            "gl_account": row.get("gl_account"),
            "status": row.get("status"),
            "invoice_amount": row.get("invoice_amount"),
            "approved_amount": row.get("approved_amount"),
            "variance": row.get("variance"),
            "detail": row.get("detail"),
        })

    rows.sort(key=lambda r: abs(_num(r["invoice_amount"])), reverse=True)
    total = sum(abs(_num(r["invoice_amount"])) for r in rows)
    return {
        "count": len(rows),
        "total_value": _money(total),
        "rows": rows[:limit],
        "truncated": len(rows) > limit,
    }


def get_tenant_history(state, query):
    """All AR rows for a tenant name or unit, exceptions and clean alike."""
    needle = (query or "").strip().lower()
    if not needle:
        return {"count": 0, "rows": [], "error": "no tenant or unit given"}

    rows = []
    for row in state.get("ar_results", []):
        haystack = "%s %s %s" % (
            row.get("tenant", ""), row.get("unit", ""), row.get("lease_id", "")
        )
        if needle in haystack.lower():
            rows.append({
                "unit": row.get("unit"),
                "tenant": row.get("tenant"),
                "property_id": row.get("property_id"),
                "status": row.get("status"),
                "amount_due": row.get("amount_due"),
                "amount_received": row.get("amount_received"),
                "variance": row.get("variance"),
                "days_late": row.get("days_late"),
                "detail": row.get("detail"),
            })

    open_count = sum(1 for r in rows if r["status"] not in CLEAN_AR)
    return {
        "count": len(rows),
        "open_exceptions": open_count,
        "rows": rows[:MAX_ROWS],
        "truncated": len(rows) > MAX_ROWS,
    }


def get_vendor_history(state, query):
    """All AP invoices for a vendor name, matched and exceptional alike."""
    needle = (query or "").strip().lower()
    if not needle:
        return {"count": 0, "rows": [], "error": "no vendor given"}

    rows = []
    for row in state.get("ap_results", []):
        if needle in (row.get("vendor", "") or "").lower():
            rows.append({
                "invoice_number": row.get("invoice_number"),
                "property_id": row.get("property_id"),
                "work_order_id": row.get("work_order_id"),
                "status": row.get("status"),
                "invoice_amount": row.get("invoice_amount"),
                "approved_amount": row.get("approved_amount"),
                "variance": row.get("variance"),
                "detail": row.get("detail"),
            })

    open_count = sum(1 for r in rows if r["status"] != "matched")
    total = sum(abs(_num(r["invoice_amount"])) for r in rows)
    return {
        "count": len(rows),
        "open_exceptions": open_count,
        "total_billed": _money(total),
        "rows": rows[:MAX_ROWS],
        "truncated": len(rows) > MAX_ROWS,
    }


def get_portfolio_summary(state):
    """Per-property units, occupancy, collections, NOI, plus the total row."""
    pf = state.get("portfolio") or {}
    props = pf.get("properties", [])
    return {
        "properties": [
            {
                "property_id": p.get("property_id"),
                "name": p.get("name"),
                "units": p.get("units"),
                "occupancy_rate": p.get("occupancy_rate"),
                "billed": p.get("billed"),
                "collected": p.get("collected"),
                "shortfall": p.get("shortfall"),
                "ar_exceptions": p.get("ar_exceptions"),
                "ap_exceptions": p.get("ap_exceptions"),
                "noi": p.get("noi"),
            }
            for p in props
        ],
        "portfolio": pf.get("portfolio", {}),
    }


def get_financials(state):
    """NOI statement with and without reconciliation, plus the delta."""
    fin = state.get("financials") or {}
    if not fin:
        return {"error": "no financials computed yet"}

    before = fin.get("without_reconciliation", {})
    after = fin.get("with_reconciliation", {})
    delta = fin.get("delta", {})
    return {
        "noi_without_reconciliation": delta.get("noi_without"),
        "noi_with_reconciliation": delta.get("noi_with"),
        "noi_impact": delta.get("noi_delta"),
        "held_for_review": delta.get("held_for_review"),
        "uncollected_receivables": delta.get("uncollected"),
        "revenue": after.get("revenue", []),
        "expenses": after.get("expenses", []),
        "total_revenue": after.get("total_revenue"),
        "total_expenses": after.get("total_expenses"),
        "balance_sheet": after.get("balance_sheet", []),
        "total_assets": after.get("total_assets"),
        "total_liabilities": after.get("total_liabilities"),
        "total_equity": after.get("total_equity"),
        "_note": "before_reconciliation_totals: revenue=%s expenses=%s"
                 % (before.get("total_revenue"), before.get("total_expenses")),
    }


# ----------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------
TOOLS = [
    {
        "name": "list_ar_exceptions",
        "description": (
            "List rent-roll and receipt exceptions (short pays, non-payments, "
            "overpayments, missed CAM escalations, unmatched receipts, vacant-unit "
            "payments). Use for questions about tenants owing money or overdue rent. "
            "Sorted by absolute variance, largest first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Filter to one status, e.g. non_payment, partial_payment, overpayment, rent_escalation_missed, unmatched_receipt.",
                },
                "property_id": {"type": "string", "description": "Filter to one property, e.g. PROP-01."},
                "min_amount": {"type": "number", "description": "Only rows with variance at or above this amount."},
            },
        },
    },
    {
        "name": "list_ap_exceptions",
        "description": (
            "List vendor-invoice exceptions (over budget, under billed, no work "
            "order, duplicate invoice, vendor mismatch). Use for questions about "
            "vendor overbilling or unsupported invoices. Sorted by amount, largest first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Filter to one status: over_budget, amount_variance, no_work_order, duplicate_invoice, vendor_mismatch.",
                },
                "property_id": {"type": "string", "description": "Filter to one property, e.g. PROP-01."},
                "min_amount": {"type": "number", "description": "Only invoices at or above this amount."},
            },
        },
    },
    {
        "name": "get_tenant_history",
        "description": (
            "Every rent-roll row for one tenant or unit, including ones that "
            "reconciled cleanly. Use to answer 'is this tenant a repeat offender' "
            "or to look up a specific unit before judging an exception."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Tenant name, unit number, or lease id."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_vendor_history",
        "description": (
            "Every invoice for one vendor, matched and exceptional alike. Use to "
            "check whether a vendor has a pattern of overbilling or repeats the "
            "same invoice."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Vendor name, partial match is fine."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_portfolio_summary",
        "description": (
            "Per-property units, occupancy, billed, collected, shortfall, exception "
            "counts and NOI, plus the portfolio total. Use for anything across "
            "properties or comparing properties."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_financials",
        "description": (
            "The NOI operating statement: revenue and expense lines by GL account, "
            "NOI with and without reconciliation, the NOI impact, amounts held for "
            "review, uncollected receivables, and the balance sheet. Use for "
            "questions about NOI, the financial statements, or the effect of review "
            "decisions."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
]

_BY_NAME = {
    "list_ar_exceptions": list_ar_exceptions,
    "list_ap_exceptions": list_ap_exceptions,
    "get_tenant_history": get_tenant_history,
    "get_vendor_history": get_vendor_history,
    "get_portfolio_summary": get_portfolio_summary,
    "get_financials": get_financials,
}


# Values a model may send to mean "no filter". Without this, a stringified
# JSON null becomes a real filter value that matches nothing -- which produced
# a confident "I could not find any tenants past due" when two existed.
_NULLISH = {"", "null", "none", "nil", "undefined", "n/a", "any", "all"}


def _clean_args(name, arguments):
    """Drop nullish values and coerce numeric strings.

    Models routinely emit {"property_id": "null", "min_amount": "0"} where the
    schema asked for an omitted optional property and a number.
    """
    cleaned = {}
    for key, value in (arguments or {}).items():
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.lower() in _NULLISH:
                continue
            value = stripped

        if key in ("min_amount", "limit"):
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            if key == "limit":
                value = int(value)

        cleaned[key] = value
    return cleaned


def call_tool(name, arguments, state):
    """Dispatch a tool call. Unknown tools and bad arguments return an error
    dict rather than raising -- a model should get feedback, not a 500."""
    fn = _BY_NAME.get(name)
    if fn is None:
        return {"error": "unknown tool %r" % name}
    try:
        return fn(state, **_clean_args(name, arguments))
    except TypeError as exc:
        return {"error": "bad arguments for %s: %s" % (name, exc)}


def schemas():
    """Tool schemas in Ollama / OpenAI function-calling shape."""
    return [{"type": "function", "function": t} for t in TOOLS]
