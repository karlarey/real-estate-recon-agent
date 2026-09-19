"""Prove the pending-queue drift is fixed: reseeding must be reproducible.

Before the fix, random.seed() ran once at import and _decide() kept drawing from
that same stream, so every reseed shifted the approved/rejected/pending mix until
a run produced an empty review queue.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import reconcile_ar  # noqa: E402
import reconcile as ap_engine  # noqa: E402
import workflow  # noqa: E402
from recon_common import load_rows  # noqa: E402

PERIOD = "2026-03"
data = lambda n: load_rows(os.path.join(HERE, "data", n))

ar = reconcile_ar.reconcile_ar(data("rent_roll.csv"), data("ar_receipts.csv"), PERIOD)
ap = ap_engine.reconcile_ap(data("ap_invoices.csv"), data("ap_work_orders.csv"))

print("Reseeding repeatedly, as the app does on every /api/demo call:")
pending_counts = []
for i in range(1, 8):
    workflow.seed_workflow(ar, ap)
    pend = workflow.pending_items(ar, ap)
    counts = workflow.summary_counts(ar, ap)
    pending_counts.append(len(pend))
    print("  call %d: pending=%d  approved=%d rejected=%d auto=%d"
          % (i, len(pend), counts["approved"], counts["rejected"], counts["auto_approved"]))

print()
print("pending per call:", pending_counts)

stable = len(set(pending_counts)) == 1
nonzero = all(c > 0 for c in pending_counts)

if not stable:
    print("FAIL  pending count drifts between reseeds")
elif not nonzero:
    print("FAIL  a reseed produced an empty queue")
else:
    print("PASS  reseeding is reproducible and the queue is never empty")

raise SystemExit(0 if (stable and nonzero) else 1)
