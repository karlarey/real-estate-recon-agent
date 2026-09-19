"""Portfolio rollup: per-property occupancy, collections, and NOI.

Takes the reconciliation output from both sides and aggregates by property, so
a single-property demo and a portfolio view read from the same code path.
"""

from collections import defaultdict

from recon_common import money

# Accounts that count as revenue vs expense in the operating statement.
REVENUE_ACCOUNTS = {"4100", "4200"}
EXPENSE_ACCOUNTS = {"5100", "5200", "5300", "5400"}


def _blank(prop_id, meta_by_id):
    meta = meta_by_id.get(prop_id, {})
    return {
        "property_id": prop_id,
        "name": meta.get("name", prop_id),
        "city": meta.get("city", ""),
        "state": meta.get("state", ""),
        "property_type": meta.get("property_type", ""),
        "units": 0,
        "occupied": 0,
        "vacant": 0,
        "occupancy_rate": 0.0,
        "billed": 0.0,
        "collected": 0.0,
        "shortfall": 0.0,
        "ar_exceptions": 0,
        "ap_invoiced": 0.0,
        "ap_approved": 0.0,
        "ap_exceptions": 0,
        "noi": 0.0,
    }


def build_summary(properties, ar_results, ap_results, workflow_status_fn=None):
    """Aggregate AR, AP and NOI into one row per property plus a portfolio total."""
    meta_by_id = {p["property_id"]: p for p in properties}
    rows = {p["property_id"]: _blank(p["property_id"], meta_by_id)
            for p in properties}

    for prop in properties:
        rows[prop["property_id"]]["units"] = int(prop.get("units") or 0)

    # ---- AR side: occupancy, billing, collection ----------------------
    for row in ar_results:
        prop = row.get("property_id", "")
        if prop not in rows:
            rows[prop] = _blank(prop, meta_by_id)
        agg = rows[prop]

        if row.get("row_type") == "unmatched_receipt":
            agg["ar_exceptions"] += 1
            agg["collected"] = money(agg["collected"] + abs(float(row["amount_received"])))
            continue

        occupancy = (row.get("occupancy_status") or "").lower()
        if occupancy == "vacant":
            agg["vacant"] += 1
        elif occupancy == "occupied":
            agg["occupied"] += 1

        due = float(row["amount_due"])
        got = float(row["amount_received"])
        agg["billed"] = money(agg["billed"] + due)
        agg["collected"] = money(agg["collected"] + got)
        if due > got:
            agg["shortfall"] = money(agg["shortfall"] + (due - got))
        if row.get("status") not in ("paid_in_full", "vacant_no_charge"):
            agg["ar_exceptions"] += 1

    # ---- AP side: approved vs billed, exceptions ---------------------
    for row in ap_results:
        prop = row.get("property_id", "")
        if prop not in rows:
            rows[prop] = _blank(prop, meta_by_id)
        agg = rows[prop]
        agg["ap_invoiced"] = money(agg["ap_invoiced"] + abs(float(row["invoice_amount"])))
        if row.get("approved_amount"):
            agg["ap_approved"] = money(
                agg["ap_approved"] + float(row["approved_amount"])
            )
        if row.get("status") != "matched":
            agg["ap_exceptions"] += 1

    # ---- NOI: collected revenue less approved operating expense -------
    for agg in rows.values():
        total_units = agg["occupied"] + agg["vacant"]
        agg["occupancy_rate"] = (
            round(100.0 * agg["occupied"] / total_units, 1) if total_units else 0.0
        )
        # Approved spend is the recognised expense; exceptions are held, not
        # posted, until a reviewer releases them.
        agg["noi"] = money(agg["collected"] - agg["ap_approved"])

    ordered = sorted(rows.values(), key=lambda r: r["property_id"])

    portfolio = {
        "property_id": "PORTFOLIO",
        "name": "Portfolio Total",
        "city": "",
        "state": "",
        "property_type": "",
        "units": sum(r["units"] for r in ordered),
        "occupied": sum(r["occupied"] for r in ordered),
        "vacant": sum(r["vacant"] for r in ordered),
        "billed": money(sum(r["billed"] for r in ordered)),
        "collected": money(sum(r["collected"] for r in ordered)),
        "shortfall": money(sum(r["shortfall"] for r in ordered)),
        "ar_exceptions": sum(r["ar_exceptions"] for r in ordered),
        "ap_invoiced": money(sum(r["ap_invoiced"] for r in ordered)),
        "ap_approved": money(sum(r["ap_approved"] for r in ordered)),
        "ap_exceptions": sum(r["ap_exceptions"] for r in ordered),
        "noi": money(sum(r["noi"] for r in ordered)),
    }
    total_units = portfolio["occupied"] + portfolio["vacant"]
    portfolio["occupancy_rate"] = (
        round(100.0 * portfolio["occupied"] / total_units, 1) if total_units else 0.0
    )

    return {"properties": ordered, "portfolio": portfolio}


def aging_buckets(ar_results, today_day=31):
    """Age unsettled balances into 0-30 / 31-60 / 61-90 / 90+ buckets.

    Days outstanding is measured from the period's grace deadline to the day the
    payment arrived; anything still unpaid ages to the end of the period.
    """
    buckets = {"current": 0.0, "1_30": 0.0, "31_60": 0.0, "61_90": 0.0, "over_90": 0.0}

    for row in ar_results:
        due = float(row["amount_due"])
        got = float(row["amount_received"])
        outstanding = due - got
        if outstanding <= 0:
            continue

        raw_days = row.get("days_late") or ""
        days = int(raw_days) if str(raw_days).strip().isdigit() else 0

        if days <= 0:
            buckets["current"] = money(buckets["current"] + outstanding)
        elif days <= 30:
            buckets["1_30"] = money(buckets["1_30"] + outstanding)
        elif days <= 60:
            buckets["31_60"] = money(buckets["31_60"] + outstanding)
        elif days <= 90:
            buckets["61_90"] = money(buckets["61_90"] + outstanding)
        else:
            buckets["over_90"] = money(buckets["over_90"] + outstanding)

    buckets["total"] = money(sum(v for k, v in buckets.items() if k != "total"))
    return buckets
