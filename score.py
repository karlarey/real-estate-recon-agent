"""Score reconciliation output against generated ground truth.

Two suites, both exiting non-zero on any disagreement:
    score_ar()  - rent-roll charges and stray receipts against ground_truth_ar
    score_ap()  - vendor invoices against ground_truth_ap

AR is compared by key (lease_id for rent-roll charges, receipt_id for stray
receipts) because the reconciler emits roll order then strays, which does not
line up positionally with ground truth. AP is compared positionally: both sides
are built in invoice-file order with duplicates appended in the same order.

Run:  python score.py
"""

import csv
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def load(path):
    if not os.path.exists(path):
        raise SystemExit("missing %s -- run the reconcilers first" % path)
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return [
            {k: (v or "").strip() for k, v in row.items() if k is not None}
            for row in csv.DictReader(handle)
        ]


def _report(title, per_status, correct, total, mismatches):
    print("")
    print(title)
    print("-" * 58)
    for label in sorted(per_status):
        hit, seen = per_status[label]
        flag = "" if hit == seen else "   <-- MISMATCH"
        print("%-22s %3d/%-3d%s" % (label, hit, seen, flag))
    print("-" * 58)
    print("accuracy               %d/%d (%.1f%%)"
          % (correct, total, 100.0 * correct / total if total else 0.0))

    if mismatches:
        print("")
        print("Misclassified:")
        for row in mismatches:
            print("  %s" % row)
        return 1

    print("all rows classified correctly")
    return 0


def score_ar(results_path=None, truth_path=None):
    """Compare AR results against ground truth, keyed per row."""
    results = load(results_path or os.path.join(HERE, "ar_results.csv"))
    truth = load(truth_path or os.path.join(HERE, "data", "ground_truth_ar.csv"))

    def key(row):
        if row.get("row_type") == "unmatched_receipt" or not row.get("lease_id"):
            return "R:" + (row.get("receipt_id") or row.get("unit") or "")
        return "L:" + row["lease_id"].upper()

    produced = {}
    for row in results:
        produced[key(row)] = row

    correct = 0
    per_status = {}
    mismatches = []

    for expected in truth:
        want = expected["expected_status"]
        bucket = per_status.setdefault(want, [0, 0])
        bucket[1] += 1

        got_row = produced.get(key(expected))
        got = got_row["status"] if got_row else "(no matching result row)"

        if got == want:
            correct += 1
            bucket[0] += 1
        else:
            mismatches.append(
                "%-8s %-12s expected %-22s got %-22s %s"
                % (expected.get("unit", ""),
                   expected.get("lease_id") or expected.get("receipt_id"),
                   want, got, expected.get("note", "")[:38])
            )

    return _report("Score: AR (rent roll vs receipts)", per_status,
                   correct, len(truth), mismatches)


def score_ap(results_path=None, truth_path=None):
    """Compare AP results against ground truth positionally."""
    results = load(results_path or os.path.join(HERE, "ap_results.csv"))
    truth = load(truth_path or os.path.join(HERE, "data", "ground_truth_ap.csv"))

    if len(results) != len(truth):
        raise SystemExit(
            "row count mismatch: ap_results has %d, ground_truth_ap has %d"
            % (len(results), len(truth))
        )

    correct = 0
    per_status = {}
    mismatches = []

    for produced, expected in zip(results, truth):
        want = expected["expected_status"]
        bucket = per_status.setdefault(want, [0, 0])
        bucket[1] += 1

        if produced["status"] == want:
            correct += 1
            bucket[0] += 1
        else:
            mismatches.append(
                "%-10s expected %-22s got %-22s %s"
                % (produced["invoice_number"], want, produced["status"],
                   produced["detail"][:38])
            )

    return _report("Score: AP (invoices vs work orders)", per_status,
                   correct, len(truth), mismatches)


def main():
    ar_rc = score_ar()
    ap_rc = score_ap()
    print("")
    return ar_rc or ap_rc


if __name__ == "__main__":
    sys.exit(main())
