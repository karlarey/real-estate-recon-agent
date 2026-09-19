"""Review advisor for the reconciliation workflow.

Two layers, always safe:

1. Deterministic rules (always on) -- a visible signal table produces a
   recommendation, confidence and a list of the factors weighed. No network.
2. Gemini enrichment (optional) -- when GOOGLE_API_KEY is set, the rationale is
   rewritten into clearer plain English. Any timeout or error falls back to the
   rules text, flagged ``degraded``.

The advisor never acts: it advises, and the reviewer still clicks approve /
reject. Every verdict exposes the signals it weighed so it can be traced and
overridden.

Usage:
    from agent import review
    verdict = review(row, context)          # rules only
    verdict = review(row, context, use_llm=True)   # + Gemini if key present
"""

import os
from collections import Counter

import env

env.load()   # pick up GOOGLE_API_KEY / GEMINI_MODEL from .env

# ----------------------------------------------------------------------
# The signal table. This IS the policy: each factor contributes signed
# weight toward the recommendation. Total > 0  => approve-lean,
# total < 0 => reject-lean; |total| drives confidence. Escalate wins when the
# amount is material regardless of direction.
# ----------------------------------------------------------------------
ESCALATE_ABOVE = 2000.0        # any AR/AP amount above this is material
MATERIAL_ABOVE = 500.0         # an amount that matters but need not escalate
SMALL_VARIANCE = 0.10          # 10% of due/approved counts as trivial

AR_FACTORS = {
    "non_payment":          -0.6,
    "partial_payment":      -0.4,
    "overpayment":          -0.3,
    "rent_escalation_missed": -0.4,
    "unmatched_receipt":    -0.8,
}
AP_FACTORS = {
    "duplicate_invoice":    -0.8,
    "no_work_order":        -0.7,
    "over_budget":          -0.4,
    "amount_variance":      -0.2,
    "vendor_mismatch":      -0.4,
}

REPEAT_WEIGHT = -0.3         # tenant/vendor already has other open exceptions
LATE_WEIGHT = -0.2           # AR past the grace window


def _history_signal(side, key, context):
    """Repeat-offender weight: more prior open exceptions => harder reject-lean."""
    history = context.get("history", {})
    counts = Counter(
        item.get("recon_status")
        for item in history.get(side, {}).get(key, [])
    )
    open_count = sum(
        n for status, n in counts.items()
        if status not in ("paid_in_full", "matched", "vacant_no_charge")
    )
    if open_count >= 2:
        return {"factor": "repeat_offender", "finding":
                "%d prior open exceptions" % open_count, "weight": REPEAT_WEIGHT}
    return {"factor": "repeat_offender", "finding": "no prior open exceptions",
            "weight": 0.0}


def review_ar(row, context=None):
    """Rules verdict for a rent-roll / receipt exception."""
    context = context or {}
    status = row.get("status", "")
    amount = abs(float(row.get("amount_due") or 0)) or abs(float(row.get("amount_received") or 0))
    due = abs(float(row.get("amount_due") or 0))
    received = abs(float(row.get("amount_received") or 0))
    days_late = row.get("days_late", "")

    signals = [
        {"factor": "status", "finding": status,
         "weight": AR_FACTORS.get(status, 0.0)},
        {"factor": "amount", "finding": "%.2f" % amount,
         "weight": 0.0},
    ]

    # Materiality: escalate large values either way. This is decisive, so it
    # carries a real weight -- showing it as 0.0 while it drives the verdict
    # would make the Explain table contradict the recommendation.
    if amount >= ESCALATE_ABOVE:
        signals.append({"factor": "materiality", "finding":
                        "amount %.2f exceeds escalation threshold — decisive"
                        % amount, "weight": -0.9})

    # Days late / aging.
    try:
        days = int(days_late) if str(days_late).strip().isdigit() else 0
    except (TypeError, ValueError):
        days = 0
    if days > 0:
        signals.append({"factor": "days_late", "finding": "%d days past grace" % days,
                        "weight": LATE_WEIGHT})
    elif status in ("non_payment", "partial_payment"):
        signals.append({"factor": "days_late", "finding": "no payment on record",
                        "weight": LATE_WEIGHT})

    # Trivial shortfall => approve-lean (waivable).
    if status == "partial_payment" and due > 0 and (due - received) / due <= SMALL_VARIANCE:
        signals.append({"factor": "variance_pct", "finding":
                        "shortfall within %.0f%% of amount due" % (SMALL_VARIANCE * 100),
                        "weight": 0.5})

    # Repeat offender.
    signals.append(_history_signal("AR", row.get("lease_id") or row.get("unit"), context))

    return _decide(amount, signals)


def review_ap(row, context=None):
    """Rules verdict for a vendor-invoice exception."""
    context = context or {}
    status = row.get("status", "")
    amount = abs(float(row.get("invoice_amount") or 0))
    approved = abs(float(row.get("approved_amount") or 0))

    signals = [
        {"factor": "status", "finding": status,
         "weight": AP_FACTORS.get(status, 0.0)},
        {"factor": "amount", "finding": "%.2f" % amount, "weight": 0.0},
    ]

    if amount >= ESCALATE_ABOVE:
        signals.append({"factor": "materiality", "finding":
                        "amount %.2f exceeds escalation threshold — decisive"
                        % amount, "weight": -0.9})

    if status in ("over_budget", "amount_variance") and approved > 0:
        pct = (amount - approved) / approved
        if abs(pct) <= SMALL_VARIANCE:
            signals.append({"factor": "variance_pct", "finding":
                            "billed within %.0f%% of approved" % (SMALL_VARIANCE * 100),
                            "weight": 0.4})
        else:
            signals.append({"factor": "variance_pct", "finding":
                            "billed %+.0f%% vs approved" % (pct * 100),
                            "weight": -0.2})

    signals.append(_history_signal("AP", row.get("invoice_number"), context))

    return _decide(amount, signals)


# Control failures are data-integrity problems: reject and request a
# correction, regardless of amount.
CONTROL_FAILURES = {
    "duplicate_invoice", "no_work_order", "unmatched_receipt", "vacant_unit_payment",
}


def _decide(amount, signals):
    """Decide by priority cascade, not raw weight sum.

    A weight sum with fixed thresholds produced non-intuitive verdicts (a 5%
    over-budget and a 2% short-pay both landed in a "escalate" dead band). A
    priority cascade is predictable and mirrors how a reviewer actually thinks:
    material first, control failures second, waivable variances third.
    """
    material = amount >= ESCALATE_ABOVE
    status = next((s["finding"] for s in signals if s["factor"] == "status"), "")
    small_variance = any(
        s["factor"] == "variance_pct" and s["weight"] > 0 for s in signals
    )

    if material:
        recommendation, confidence = "escalate", 0.9
    elif status in CONTROL_FAILURES:
        recommendation, confidence = "reject", 0.95
    elif small_variance:
        recommendation, confidence = "approve", 0.6
    else:
        recommendation, confidence = "escalate", 0.5

    rationale = _rationale(amount, signals, recommendation, material, status,
                           small_variance)
    next_step = _next_step(recommendation)

    return {
        "recommendation": recommendation,
        "confidence": confidence,
        "rationale": rationale,
        "signals": signals,
        "next_step": next_step,
        "source": "rules",
    }


def _rationale(amount, signals, recommendation, material, status, small_variance):
    if material:
        return ("%s of %.2f is material; escalate for manual review rather "
                "than approve in bulk." % (status, amount))
    if recommendation == "reject":
        return ("%s of %.2f is a data-quality or control failure; reject and "
                "request a correction." % (status, amount))
    if small_variance:
        return ("%s of %.2f is within tolerance; low risk to accept as-is."
                % (status, amount))
    return ("%s of %.2f needs a closer look; escalate to a manager."
            % (status, amount))


def _next_step(recommendation):
    return {
        "approve": "Accept and post after confirming the variance is within policy.",
        "reject": "Reject and request a revised charge or receipt from the counterparty.",
        "escalate": "Escalate to the property manager for manual review.",
    }[recommendation]


def review(row, context=None, use_llm=True):
    """Advisor entry point: run the rules, then enrich with Gemini if possible."""
    side = row.get("side") or row.get("row_type", "")
    if side in ("AR", "rent_roll", "unmatched_receipt"):
        verdict = review_ar(row, context)
    else:
        verdict = review_ap(row, context)

    if use_llm:
        verdict = _enrich_with_gemini(row, verdict)

    return verdict


# ----------------------------------------------------------------------
# Gemini enrichment (optional, graceful)
# ----------------------------------------------------------------------
def _client():
    """Lazily build a Gemini client from GOOGLE_API_KEY, or None."""
    key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if not key:
        return None
    try:
        from google import genai
    except ImportError:
        return None
    return genai.Client(api_key=key)


def _enrich_with_gemini(row, verdict):
    """Rewrite the rationale with Gemini; fall back to rules text on any failure."""
    client = _client()
    if client is None:
        verdict["source"] = "rules (gemini unavailable)"
        return verdict

    model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
    prompt = (
        "You are a property-accounting reviewer. Rewrite the following "
        "reconciliation exception rationale in one clear sentence, keeping the "
        "recommendation. Do not add facts not present.\n"
        "Exception: %s\nRecommendation: %s\nRationale: %s"
        % (row.get("detail") or row.get("status"), verdict["recommendation"],
           verdict["rationale"])
    )
    try:
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config={"max_output_tokens": 200},
        )
        text = (response.text or "").strip()
        if text:
            verdict["rationale"] = text
            verdict["source"] = "gemini"
            return verdict
    except Exception:
        pass

    verdict["source"] = "rules (gemini unavailable)"
    return verdict


def build_context(ar_results, ap_results):
    """Assemble repeat-offender history for the advisor.

    Groups prior open exceptions by tenant/vendor key so the repeat_offender
    signal has real data behind it.
    """
    ar_hist = {}
    for row in ar_results:
        key = row.get("lease_id") or row.get("unit")
        if not key:
            continue
        ar_hist.setdefault(key, []).append({"recon_status": row.get("status")})

    ap_hist = {}
    for row in ap_results:
        key = row.get("invoice_number")
        if not key:
            continue
        ap_hist.setdefault(key, []).append({"recon_status": row.get("status")})

    return {"history": {"AR": ar_hist, "AP": ap_hist}}
