"""Workflow state: one review queue covering both AR and AP exceptions.

Keys are namespaced so the two sides share a queue without colliding:
    AR-<lease_id>       rent-roll charge exception
    AR-<receipt_id>     unmatched receipt
    AP-<invoice_number> vendor invoice exception

In-memory only (no persistence). Seeded with a realistic decision mix so the UI
shows something meaningful before anyone clicks.
"""

import random
from datetime import datetime

REVIEWERS = ["Alice Chen", "Bob Martinez", "Carol Smith", "Dan Wilson"]
random.seed(99)

_workflow: dict = {}

# AR statuses that need no human review.
CLEAN_AR = {"paid_in_full", "vacant_no_charge"}


def ar_key(row):
    """Stable workflow key for an AR result row."""
    if row.get("row_type") == "unmatched_receipt":
        return "AR-" + (row.get("receipt_id") or row.get("unit") or "unknown")
    return "AR-" + (row.get("lease_id") or row.get("unit") or "unknown")


def ap_key(row):
    """Stable workflow key for an AP result row.

    A duplicate invoice reuses its original's invoice number, so keying on that
    alone would collide -- the duplicate's review would overwrite the original's.
    Duplicates get a row-suffixed key so both stay independently reviewable.
    """
    base = "AP-" + (row.get("invoice_number") or "unknown")
    if row.get("status") == "duplicate_invoice":
        return "%s-dup%s" % (base, row.get("invoice_row", ""))
    return base


def _decide(exception):
    """Assign an initial decision: 60% approved, 20% rejected, 20% pending."""
    if not exception:
        return {
            "workflow_status": "auto_approved",
            "reviewer": "system",
            "note": "reconciled clean — no review required",
            "decided_at": "2026-01-01T00:00:00",
        }
    roll = random.random()
    if roll < 0.60:
        return {
            "workflow_status": "approved",
            "reviewer": random.choice(REVIEWERS),
            "note": "Reviewed and accepted.",
            "decided_at": "",
        }
    if roll < 0.80:
        return {
            "workflow_status": "rejected",
            "reviewer": random.choice(REVIEWERS),
            "note": "Rejected — follow up required.",
            "decided_at": "",
        }
    return {"workflow_status": "pending", "reviewer": "", "note": "", "decided_at": ""}


def seed_workflow(ar_results, ap_results):
    """Populate workflow state from both reconciliation result sets.

    Reseeds the RNG here rather than relying on the import-time seed: _decide()
    draws from the same stream, so without this every reseed would continue from
    wherever the last one stopped and the approved/rejected/pending mix would
    drift per call -- eventually producing an empty review queue.
    """
    random.seed(99)
    _workflow.clear()
    for row in ar_results:
        _workflow[ar_key(row)] = _decide(row.get("status") not in CLEAN_AR)
    for row in ap_results:
        _workflow[ap_key(row)] = _decide(row.get("status") != "matched")


def get_workflow_status(key):
    return _workflow.get(key, {
        "workflow_status": "pending",
        "reviewer": "",
        "note": "",
        "decided_at": "",
    })


def pending_items(ar_results, ap_results):
    """Exception rows from both sides still awaiting a reviewer decision."""
    out = []

    for row in ar_results:
        if row.get("status") in CLEAN_AR:
            continue
        key = ar_key(row)
        if get_workflow_status(key)["workflow_status"] != "pending":
            continue
        amount = abs(float(row["amount_due"] or 0)) or abs(float(row["amount_received"] or 0))
        out.append({
            "key": key,
            "side": "AR",
            "reference": "%s / %s" % (row.get("unit", ""), row.get("tenant") or "—"),
            "property_id": row.get("property_id", ""),
            "amount": "%.2f" % amount,
            "recon_status": row["status"],
            "detail": row.get("detail", ""),
            "workflow_status": "pending",
            "reviewer": "",
            "note": "",
            "review": get_review(key),
        })

    for row in ap_results:
        if row.get("status") == "matched":
            continue
        key = ap_key(row)
        if get_workflow_status(key)["workflow_status"] != "pending":
            continue
        out.append({
            "key": key,
            "side": "AP",
            "reference": "%s / %s" % (row.get("invoice_number", ""), row.get("vendor", "")),
            "property_id": row.get("property_id", ""),
            "amount": "%.2f" % abs(float(row["invoice_amount"])),
            "recon_status": row["status"],
            "detail": row.get("detail", ""),
            "workflow_status": "pending",
            "reviewer": "",
            "note": "",
            "review": get_review(key),
        })

    return out


def _labelled(ar_results, ap_results):
    """(key, needs_no_review, amount) for every reconciled item."""
    out = []
    for row in ar_results:
        amount = abs(float(row["amount_due"] or 0))
        if amount == 0:
            amount = abs(float(row["amount_received"] or 0))
        out.append((ar_key(row), row.get("status") in CLEAN_AR, amount))
    for row in ap_results:
        out.append((
            ap_key(row), row.get("status") == "matched",
            abs(float(row["invoice_amount"])),
        ))
    return out


# ----------------------------------------------------------------------
# Agent reviews, stored alongside workflow state so the UI can render a
# recommendation without a second round-trip.
# ----------------------------------------------------------------------
_reviews: dict = {}


def set_review(key, verdict):
    _reviews[key] = verdict
    return verdict


def get_review(key):
    return _reviews.get(key)


def seed_reviews(ar_results, ap_results, reviewer):
    """Run the advisor over every exception once at seed time.

    Uses the caller-supplied `reviewer` (agent.review) so workflow.py has no
    import dependency on the agent module and the rules path stays offline-safe.
    """
    _reviews.clear()
    context = None
    try:
        import agent as _agent  # local, so a missing module can't break seeding
        context = _agent.build_context(ar_results, ap_results)
    except ImportError:
        pass

    for row in ar_results:
        if row.get("status") in CLEAN_AR:
            continue
        key = ar_key(row)
        enriched = _row_for_review(row, "AR")
        _reviews[key] = reviewer(enriched, context, use_llm=False)

    for row in ap_results:
        if row.get("status") == "matched":
            continue
        key = ap_key(row)
        enriched = _row_for_review(row, "AP")
        _reviews[key] = reviewer(enriched, context, use_llm=False)


def _row_for_review(row, side):
    """Normalise a result row into the shape agent.review expects."""
    if side == "AR":
        return {
            "side": "AR",
            "status": row.get("status"),
            "amount_due": row.get("amount_due"),
            "amount_received": row.get("amount_received"),
            "days_late": row.get("days_late"),
            "lease_id": row.get("lease_id"),
            "unit": row.get("unit"),
            "detail": row.get("detail"),
        }
    return {
        "side": "AP",
        "status": row.get("status"),
        "invoice_amount": row.get("invoice_amount"),
        "approved_amount": row.get("approved_amount"),
        "invoice_number": row.get("invoice_number"),
        "detail": row.get("detail"),
    }


def summary_counts(ar_results, ap_results):
    counts = {"auto_approved": 0, "approved": 0, "rejected": 0, "pending": 0}
    approved_value = held_value = 0.0

    labelled = _labelled(ar_results, ap_results)
    for key, clean, amount in labelled:
        state = get_workflow_status(key)["workflow_status"]
        counts[state] = counts.get(state, 0) + 1
        if clean or state == "approved":
            approved_value += amount
        elif state in ("rejected", "pending"):
            held_value += amount

    return {
        "auto_approved": counts["auto_approved"],
        "approved": counts["approved"],
        "rejected": counts["rejected"],
        "pending": counts["pending"],
        "total_items": len(labelled),
        "exceptions": sum(1 for _, clean, _ in labelled if not clean),
        "approved_value": round(approved_value, 2),
        "held_value": round(held_value, 2),
    }


def approve(key, reviewer, note=""):
    return _set(key, "approved", reviewer, note)


def reject(key, reviewer, note=""):
    return _set(key, "rejected", reviewer, note)


def _set(key, status, reviewer, note):
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = _workflow.setdefault(key, {})
    entry.update(
        workflow_status=status, reviewer=reviewer, note=note, decided_at=now,
    )
    return dict(entry)
