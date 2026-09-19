"""Test the HTTP API against a running server.

Covers: upload page, /health, /api/demo, AR and AP multipart uploads, portfolio
rollup, workflow endpoints, and the NOI financial comparison.

Usage:
    python test_api.py           # expects server on 127.0.0.1:8000
    python test_api.py 9000      # custom port
"""

import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = sys.argv[1] if len(sys.argv) > 1 else "8000"
BASE = "http://127.0.0.1:%s" % PORT
failures = []


def check(label, cond, detail=""):
    print("%-48s %s" % (label, "PASS" if cond else "FAIL"))
    if not cond:
        failures.append("%s: %s" % (label, detail))


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=20) as r:
        return r.status, json.loads(r.read().decode())


def post_json(path, body, timeout=20):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def post_multipart(path, files):
    boundary = "----reconBoundary7d1a"
    body = bytearray()
    for field, filename, content in files:
        body += ("--%s\r\n" % boundary).encode()
        body += (
            'Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
            % (field, filename)
        ).encode()
        body += b"Content-Type: text/csv\r\n\r\n" + content + b"\r\n"
    body += ("--%s--\r\n" % boundary).encode()
    req = urllib.request.Request(
        BASE + path, data=bytes(body),
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def read(name):
    with open(os.path.join(HERE, "data", name), "rb") as f:
        return f.read()


def main():
    # ── 1. Page ──────────────────────────────────────────────────────
    with urllib.request.urlopen(BASE + "/", timeout=20) as r:
        status, html = r.status, r.read().decode()
    for tab in ["Portfolio", "Rent Roll / AR", "Vendor Invoices / AP",
                "Workflow Review", "Financials (NOI)"]:
        check("page has %s tab" % tab, tab in html)
    check("GET / returns 200", status == 200)

    # ── 2. Health ────────────────────────────────────────────────────
    status, body = get("/health")
    check("GET /health returns 200", status == 200)
    check("health payload ok", body.get("status") == "ok")

    # ── 3. Demo bundle ───────────────────────────────────────────────
    status, text = post_multipart("/api/demo", [])
    check("POST /api/demo returns 200", status == 200, text[:200])
    demo = json.loads(text) if status == 200 else {}
    ars = demo.get("ar", {}).get("summary", {})
    aps = demo.get("ap", {}).get("summary", {})
    check("demo AR expected charges is 39", ars.get("expected_charges") == 39,
          str(ars.get("expected_charges")))
    check("demo AR exceptions > 0", (ars.get("exceptions") or 0) > 0)
    check("demo AP invoices is 35", aps.get("invoices") == 35, str(aps.get("invoices")))
    check("demo AP exceptions > 0", (aps.get("exceptions") or 0) > 0)

    # ── 4. Portfolio rollup ──────────────────────────────────────────
    pf = demo.get("portfolio", {})
    props = pf.get("properties", [])
    check("portfolio has 3 properties", len(props) == 3, str(len(props)))
    check("portfolio total present", pf.get("portfolio", {}).get("property_id") == "PORTFOLIO")
    check("portfolio units == 38", pf.get("portfolio", {}).get("units") == 38,
          str(pf.get("portfolio", {}).get("units")))
    check("portfolio NOI present", "noi" in pf.get("portfolio", {}))
    check("aging buckets present", "current" in demo.get("aging", {}))

    # ── 5. Per-property NOI is not just the total repeated ───────────
    if len(props) >= 2:
        check("per-property NOI differs across properties",
              props[0].get("noi") != props[1].get("noi"),
              "%s vs %s" % (props[0].get("noi"), props[1].get("noi")))

    # ── 6. Uploads ───────────────────────────────────────────────────
    status, text = post_multipart("/api/reconcile/ar", [
        ("rent_roll", "rent_roll.csv", read("rent_roll.csv")),
        ("receipts", "ar_receipts.csv", read("ar_receipts.csv")),
    ])
    check("POST /api/reconcile/ar returns 200", status == 200, text[:200])
    if status == 200:
        uploaded = json.loads(text)
        check("AR upload matches demo summary",
              uploaded["ar"]["summary"] == ars)

    status, text = post_multipart("/api/reconcile/ap", [
        ("work_orders", "ap_work_orders.csv", read("ap_work_orders.csv")),
        ("invoices", "ap_invoices.csv", read("ap_invoices.csv")),
    ])
    check("POST /api/reconcile/ap returns 200", status == 200, text[:200])

    # ── 7. Bad uploads ───────────────────────────────────────────────
    status, text = post_multipart("/api/reconcile/ar", [
        ("rent_roll", "bad.csv", b"lease_id,property_id\nLSE-1,PROP-01\n"),
        ("receipts", "ar_receipts.csv", read("ar_receipts.csv")),
    ])
    check("AR missing column rejected with 400", status == 400, str(status))
    check("AR 400 names the missing column", "unit" in text, text[:200])

    bad_receipts = (
        b"receipt_id,property_id,unit,tenant,period,amount_received,date_received\n"
        b"RCP-JUNK,PROP-01,UT-1,Test,2026-03,not-a-number,2026-03-05\n"
    )
    status, text = post_multipart("/api/reconcile/ar", [
        ("rent_roll", "rent_roll.csv", read("rent_roll.csv")),
        ("receipts", "bad.csv", bad_receipts),
    ])
    check("AR non-numeric rejected with 400", status == 400, str(status))
    check("AR 400 explains the bad value", "not a number" in text, text[:200])

    status, _ = get("/health")
    check("server survived the bad requests", status == 200)

    # ── 8. Workflow ──────────────────────────────────────────────────
    status, body = get("/api/workflow/summary")
    check("GET /api/workflow/summary returns 200", status == 200)
    for k in ["auto_approved", "approved", "rejected", "pending", "held_value"]:
        check("workflow summary has %s" % k, k in body)

    status, body = get("/api/workflow/pending")
    check("GET /api/workflow/pending returns 200", status == 200)
    check("pending has integer count", isinstance(body.get("count"), int))
    pending = body.get("pending", [])
    check("pending rows carry a side tag", all("side" in r for r in pending) if pending else True)

    if pending:
        key = pending[0]["key"]
        status, body = post_json("/api/workflow/%s/approve" % key,
                                 {"reviewer": "Test Reviewer", "note": "Approved in test"})
        check("approve returns 200", status == 200, str(status))
        check("approve sets workflow_status=approved",
              body.get("workflow_status") == "approved", str(body))
        check("approve preserves reviewer",
              body.get("reviewer") == "Test Reviewer", str(body))
    else:
        check("AR/AP queue had a pending row to approve", False, "queue empty")

    # ── 9. Financials ────────────────────────────────────────────────
    status, body = get("/api/financials/demo")
    check("GET /api/financials/demo returns 200", status == 200, str(status))
    for k in ["without_reconciliation", "with_reconciliation", "delta"]:
        check("financials has %s" % k, k in body)
    before = body.get("without_reconciliation", {})
    after = body.get("with_reconciliation", {})
    check("without_recon has revenue lines", bool(before.get("revenue")))
    check("with_recon has revenue lines", bool(after.get("revenue")))
    check("both statements report NOI",
          "noi" in before and "noi" in after)
    check("delta has noi_delta", "noi_delta" in body.get("delta", {}))
    check("delta has held_for_review", "held_for_review" in body.get("delta", {}))
    check("with_recon reports uncollected receivables",
          after.get("uncollected", 0) > 0,
          str(after.get("uncollected")))
    check("reconciled NOI differs from naive NOI",
          body["delta"]["noi_without"] != body["delta"]["noi_with"],
          "%s vs %s" % (body["delta"]["noi_without"], body["delta"]["noi_with"]))

    # ── 10. Balance sheet ────────────────────────────────────────────
    for label, stmt in (("without", before), ("with", after)):
        for k in ["total_assets", "total_liabilities", "total_equity", "balance_sheet"]:
            check("%s statement has %s" % (label, k), k in stmt)

        assets = stmt.get("total_assets", 0)
        liab_eq = stmt.get("total_liabilities", 0) + stmt.get("total_equity", 0)
        check("%s balance sheet balances" % label, abs(assets - liab_eq) < 0.01,
              "assets %s vs liabilities+equity %s" % (assets, liab_eq))

        types = {r.get("type") for r in stmt.get("balance_sheet", [])}
        check("%s statement has equity line" % label, "Equity" in types, str(types))

    names = [r["name"] for r in after.get("balance_sheet", [])]
    check("balance sheet carries real-estate accounts",
          any("Tenant Receivables" in n for n in names), str(names))

    # ── 11. Agent ─────────────────────────────────────────────────────
    status, body = get("/api/workflow/pending")
    pending = body.get("pending", [])
    check("pending rows carry a review", any(r.get("review") for r in pending),
          "no pending row had a review attached")
    if pending:
        first_key = pending[0]["key"]

        status, body = get("/api/agent/rationale/%s" % first_key)
        check("GET /api/agent/rationale returns 200", status == 200, str(status))
        check("rationale has recommendation",
              "recommendation" in body.get("review", {}), str(body))
        check("rationale has signals", bool(body.get("review", {}).get("signals")))
        check("rationale has a source field", "source" in body.get("review", {}))

        status, body = post_json("/api/agent/review/%s" % first_key, {})
        check("POST /api/agent/review returns 200", status == 200, str(status))
        check("review returns a verdict",
              body.get("review", {}).get("recommendation") in
              ("approve", "reject", "escalate"), str(body))

    status, body = post_json("/api/agent/run-all", {})
    check("POST /api/agent/run-all returns 200", status == 200, str(status))
    check("run-all covers pending count",
          body.get("count", 0) == len(body.get("reviewed", [])), str(body))
    check("run-all verdicts are sane",
          all(r["review"]["recommendation"] in ("approve", "reject", "escalate")
              for r in body.get("reviewed", [])), str(body)[:200])

    # ── 12. Chat assistant ────────────────────────────────────────────
    status, body = get("/api/chat/status")
    check("GET /api/chat/status returns 200", status == 200, str(status))
    check("chat status reports availability", "available" in body, str(body))
    check("chat status names the model", bool(body.get("model")), str(body))
    check("chat status gives a hint when offline",
          body.get("available") or bool(body.get("hint")), str(body))

    status, body = post_json("/api/chat", {"question": "which tenants are past due?"}, timeout=90)
    check("POST /api/chat returns 200", status == 200, str(status))
    check("chat returns an answer field", "answer" in body, str(body)[:160])
    check("chat reports its source", "source" in body, str(body)[:160])
    check("chat never 500s when the model is down",
          isinstance(body.get("answer"), str) and bool(body["answer"]),
          str(body)[:160])

    status, body = post_json("/api/chat", {"question": ""})
    check("empty question is handled", status == 200, str(status))

    print("")
    if failures:
        print("%d check(s) failed:" % len(failures))
        for item in failures:
            print("  " + item)
        return 1
    print("all API checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
