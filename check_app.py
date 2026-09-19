"""Smoke-check that app.py imports and the bundle carries every tab's data.

Written as a file because this shell mangles inline `python -c` quotes.

    python check_app.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app  # noqa: E402

print("app imports OK")

app.demo()
bundle = app._bundle()
print("bundle keys:      %s" % sorted(bundle.keys()))
print("ar exceptions:    %s" % bundle["ar"]["summary"]["exceptions"])
print("ap exceptions:    %s" % bundle["ap"]["summary"]["exceptions"])
print("portfolio NOI:    %s" % bundle["portfolio"]["portfolio"]["noi"])
print("pending rows:     %d" % len(bundle["pending"]))
print("properties:       %d" % len(bundle["portfolio"]["properties"]))

fin = bundle["financials"]
print("financials keys:  %s" % sorted(fin.keys()))
print("NOI without recon: %s" % fin["delta"]["noi_without"])
print("NOI with recon:    %s" % fin["delta"]["noi_with"])
print("NOI impact:        %s" % fin["delta"]["noi_delta"])

before_rev = [r["name"] for r in fin["without_reconciliation"]["revenue"]]
after_exp = [r["name"] for r in fin["with_reconciliation"]["expenses"]]
bs = [r["name"] for r in fin["with_reconciliation"]["balance_sheet"]]
print("revenue accounts: %s" % before_rev)
print("expense accounts: %s" % after_exp)
print("balance sheet:    %s" % bs)

# Both views must balance. Equity is the plug, so a mismatch means the asset or
# liability lines disagree with the P&L.
for label in ("without_reconciliation", "with_reconciliation"):
    stmt = fin[label]
    assets = stmt["total_assets"]
    liab_eq = stmt["total_liabilities"] + stmt["total_equity"]
    print("%-22s assets %.2f  liab+equity %.2f" % (label + ":", assets, liab_eq))
    assert abs(assets - liab_eq) < 0.01, "%s does not balance" % label

before_noi = fin["delta"]["noi_without"]
after_noi = fin["delta"]["noi_with"]
ratio = abs(min(before_noi, after_noi)) / fin["without_reconciliation"]["total_revenue"]
print("opex/NOI sanity: NOI %.2f / %.2f" % (before_noi, after_noi))
assert ratio < 1.0, "NOI magnitude exceeds revenue — opex ratio is unrealistic"
assert after_noi > before_noi, "reconciliation should improve NOI, not worsen it"

assert bundle["pending"], "workflow tab would render empty"
assert fin["delta"]["noi_without"] != fin["delta"]["noi_with"], "NOI delta is flat"
assert any("Tenant Receivables" in n for n in bs), "receivables not on balance sheet"
assert any("Equity" == r.get("type") for r in fin["with_reconciliation"]["balance_sheet"]), \
    "no equity line on the balance sheet"

# Every pending item must carry an advisor review with signals exposed.
for item in bundle["pending"]:
    review = item.get("review")
    assert review, "pending item %s has no advisor review" % item["key"]
    assert review.get("recommendation") in ("approve", "reject", "escalate"), \
        "bad recommendation for %s" % item["key"]
    assert review.get("signals"), "no signals exposed for %s" % item["key"]
print("advisor reviews attached to all %d pending items" % len(bundle["pending"]))

print("")
print("all app smoke checks passed")
