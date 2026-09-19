"""Real-estate reconciliation service: rent roll, AR, AP, NOI.

Serves a four-tab single-page app and a JSON API.

    GET  /                          upload page
    GET  /health                    liveness probe
    POST /api/demo                  reconcile bundled data (AR + AP), seed workflow
    POST /api/reconcile/ar          multipart: rent_roll=<csv>, receipts=<csv>
    POST /api/reconcile/ap          multipart: work_orders=<csv>, invoices=<csv>
    GET  /api/ar/results            AR exception rows
    GET  /api/ap/results            AP exception rows
    GET  /api/portfolio/summary     per-property rollup + portfolio total
    GET  /api/workflow/summary      review queue counts
    GET  /api/workflow/pending      exceptions awaiting a decision
    POST /api/workflow/<key>/approve
    POST /api/workflow/<key>/reject
    GET  /api/financials/demo       NOI before vs after reconciliation
    GET  /api/chat/status           whether the local model is reachable
    POST /api/chat                  ask the assistant about the data

Run locally:  uvicorn app:app --reload --port 8000
In Docker:    docker run -p 8000:8000 invoice-recon-agent
"""

import csv
import io
import os

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

import financials
import portfolio
import reconcile as ap_engine
import reconcile_ar as ar_engine
import workflow
import agent_chat
from recon_common import DataError, load_rows

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE_DIR = os.path.join(HERE, "data")
PERIOD = "2026-03"

RENT_ROLL_COLUMNS = [
    "lease_id", "property_id", "unit", "tenant", "monthly_rent",
    "cam_monthly", "other_charges", "total_monthly_due", "occupancy_status",
]
RECEIPT_COLUMNS = [
    "receipt_id", "property_id", "unit", "tenant", "period",
    "amount_received", "date_received",
]
WORK_ORDER_COLUMNS = [
    "work_order_id", "property_id", "vendor", "gl_account",
    "expense_category", "approved_amount",
]
INVOICE_COLUMNS = [
    "invoice_number", "property_id", "vendor", "amount",
]

app = FastAPI(title="Real Estate Reconciliation Agent", version="3.0.0")

# ----------------------------------------------------------------------
# Session state (in-memory; a restart clears it)
# ----------------------------------------------------------------------
_state = {
    "properties": [],
    "rent_roll": [],
    "ar_results": [],
    "ap_results": [],
    "period": PERIOD,
}


def _properties():
    return _state["properties"] or load_rows(os.path.join(SAMPLE_DIR, "properties.csv"))


# ----------------------------------------------------------------------
# Parsing / validation
# ----------------------------------------------------------------------
def rows_from_bytes(raw, label, required):
    """Parse CSV bytes, rejecting files missing required columns."""
    if not raw:
        raise HTTPException(400, "%s: file is empty" % label)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "%s: not a UTF-8 text file" % label)

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(400, "%s: no header row found" % label)

    present = {(name or "").strip().lower() for name in reader.fieldnames}
    missing = [c for c in required if c not in present]
    if missing:
        raise HTTPException(
            400,
            "%s: missing column(s) %s (found: %s)"
            % (label, ", ".join(missing), ", ".join(sorted(present))),
        )

    rows = [
        {k: (v or "").strip() for k, v in row.items() if k is not None}
        for row in reader
    ]
    if not rows:
        raise HTTPException(400, "%s: contains a header but no data rows" % label)
    return rows


@app.exception_handler(DataError)
def handle_data_error(request: Request, exc: DataError):
    """A malformed numeric cell is the caller's problem, not a crash."""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# ----------------------------------------------------------------------
# Response shaping
# ----------------------------------------------------------------------
def _ar_summary(results):
    counts = {}
    billed = collected = shortfall = 0.0
    for row in results:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        if row["row_type"] == "unmatched_receipt":
            continue
        due = float(row["amount_due"])
        got = float(row["amount_received"])
        billed += due
        collected += got
        shortfall += max(0.0, due - got)

    exceptions = sum(
        1 for r in results
        if r["status"] not in ("paid_in_full", "vacant_no_charge")
    )
    return {
        "expected_charges": len(results),
        "exceptions": exceptions,
        "billed": round(billed, 2),
        "collected": round(collected, 2),
        "shortfall": round(shortfall, 2),
        "collection_rate": round(100.0 * collected / billed, 1) if billed else 0.0,
        "by_status": counts,
    }


def _ap_summary(results):
    counts = {}
    billed = approved = 0.0
    for row in results:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        billed += abs(float(row["invoice_amount"]))
        if row["approved_amount"]:
            approved += float(row["approved_amount"])

    return {
        "invoices": len(results),
        "exceptions": sum(1 for r in results if r["status"] != "matched"),
        "total_billed": round(billed, 2),
        "total_approved": round(approved, 2),
        "variance": round(billed - approved, 2),
        "by_status": counts,
    }


def _bundle():
    """Current reconcile + workflow + portfolio view."""
    ar = _state["ar_results"]
    ap = _state["ap_results"]
    return {
        "period": _state["period"],
        "ar": {"summary": _ar_summary(ar), "results": ar},
        "ap": {"summary": _ap_summary(ap), "results": ap},
        "portfolio": portfolio.build_summary(_properties(), ar, ap),
        "aging": portfolio.aging_buckets(ar),
        "workflow": workflow.summary_counts(ar, ap),
        "pending": workflow.pending_items(ar, ap),
        "financials": _financials_view(),
    }


def _financials_view():
    """NOI comparison reflecting the current workflow decisions."""
    comparison = financials.build_comparison(
        _state["ar_results"], _state["ap_results"],
        workflow.get_workflow_status, workflow.ar_key, workflow.ap_key,
    )
    comparison["properties"] = portfolio.build_summary(
        _properties(), _state["ar_results"], _state["ap_results"]
    )
    return comparison


def _seed():
    """Seed workflow decisions and advisor reviews from current results."""
    import agent  # local import; the rules path is offline-safe

    workflow.seed_workflow(_state["ar_results"], _state["ap_results"])
    workflow.seed_reviews(
        _state["ar_results"], _state["ap_results"], agent.review
    )


def _review_pending_items(use_llm=False):
    """(Re)run the advisor over every pending item, mutating stored reviews."""
    import agent

    context = agent.build_context(_state["ar_results"], _state["ap_results"])
    reviewed = []
    for item in workflow.pending_items(_state["ar_results"], _state["ap_results"]):
        row = _row_for_key(item["key"])
        verdict = agent.review(row, context, use_llm=use_llm)
        workflow.set_review(item["key"], verdict)
        reviewed.append({"key": item["key"], "review": verdict})
    return reviewed


def _row_for_key(key):
    """Look up the underlying result row for a workflow key."""
    for row in _state["ar_results"]:
        if workflow.ar_key(row) == key:
            return workflow._row_for_review(row, "AR")
    for row in _state["ap_results"]:
        if workflow.ap_key(row) == key:
            return workflow._row_for_review(row, "AP")
    # Unknown key: fall back to a minimal shape the advisor can still score.
    return {"side": key[:2], "status": "unknown", "detail": "",
            "amount_due": "0", "amount_received": "0",
            "invoice_amount": "0", "approved_amount": "",
            "days_late": "", "lease_id": key, "unit": "",
            "invoice_number": key}


# ----------------------------------------------------------------------
# Core endpoints
# ----------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/demo")
def demo():
    """Reconcile the bundled rent roll, receipts, work orders and invoices."""
    try:
        roll = load_rows(os.path.join(SAMPLE_DIR, "rent_roll.csv"))
        receipts = load_rows(os.path.join(SAMPLE_DIR, "ar_receipts.csv"))
        orders = load_rows(os.path.join(SAMPLE_DIR, "ap_work_orders.csv"))
        invoices = load_rows(os.path.join(SAMPLE_DIR, "ap_invoices.csv"))
    except SystemExit as exc:
        raise HTTPException(500, str(exc))

    _state["properties"] = load_rows(os.path.join(SAMPLE_DIR, "properties.csv"))
    _state["rent_roll"] = roll
    _state["ar_results"] = ar_engine.reconcile_ar(roll, receipts, PERIOD)
    _state["ap_results"] = ap_engine.reconcile_ap(invoices, orders)
    _state["period"] = PERIOD

    _seed()
    return _bundle()


@app.post("/api/reconcile/ar")
async def reconcile_ar_upload(
    rent_roll: UploadFile = File(...),
    receipts: UploadFile = File(...),
):
    roll = rows_from_bytes(await rent_roll.read(), "rent roll", RENT_ROLL_COLUMNS)
    paid = rows_from_bytes(await receipts.read(), "receipts", RECEIPT_COLUMNS)
    period = paid[0].get("period") or PERIOD

    _state["rent_roll"] = roll
    _state["ar_results"] = ar_engine.reconcile_ar(roll, paid, period)
    _state["period"] = period
    _seed()
    return _bundle()


@app.post("/api/reconcile/ap")
async def reconcile_ap_upload(
    work_orders: UploadFile = File(...),
    invoices: UploadFile = File(...),
):
    orders = rows_from_bytes(await work_orders.read(), "work orders", WORK_ORDER_COLUMNS)
    bills = rows_from_bytes(await invoices.read(), "invoices", INVOICE_COLUMNS)

    _state["ap_results"] = ap_engine.reconcile_ap(bills, orders)
    _seed()
    return _bundle()


@app.get("/api/ar/results")
def ar_results():
    return {"summary": _ar_summary(_state["ar_results"]),
            "results": _state["ar_results"]}


@app.get("/api/ap/results")
def ap_results():
    return {"summary": _ap_summary(_state["ap_results"]),
            "results": _state["ap_results"]}


@app.get("/api/portfolio/summary")
def portfolio_summary():
    return {
        **portfolio.build_summary(_properties(), _state["ar_results"], _state["ap_results"]),
        "aging": portfolio.aging_buckets(_state["ar_results"]),
    }


# ----------------------------------------------------------------------
# Workflow
# ----------------------------------------------------------------------
@app.get("/api/workflow/summary")
def workflow_summary():
    return workflow.summary_counts(_state["ar_results"], _state["ap_results"])


@app.get("/api/workflow/pending")
def workflow_pending():
    pending = workflow.pending_items(_state["ar_results"], _state["ap_results"])
    return {"pending": pending, "count": len(pending)}


@app.post("/api/workflow/{key}/approve")
async def workflow_approve(key: str, request: Request):
    body = (await request.json()) or {}
    result = workflow.approve(key, body.get("reviewer", "unknown"), body.get("note", ""))
    return {**result, "key": key}


@app.post("/api/workflow/{key}/reject")
async def workflow_reject(key: str, request: Request):
    body = (await request.json()) or {}
    result = workflow.reject(key, body.get("reviewer", "unknown"), body.get("note", ""))
    return {**result, "key": key}


# ----------------------------------------------------------------------
# Agent
# ----------------------------------------------------------------------
@app.post("/api/agent/review/{key}")
async def agent_review(key: str):
    """Run the advisor for one exception and store the verdict."""
    import agent

    row = _row_for_key(key)
    context = agent.build_context(_state["ar_results"], _state["ap_results"])
    verdict = agent.review(row, context, use_llm=True)
    workflow.set_review(key, verdict)
    return {"key": key, "review": verdict}


@app.get("/api/agent/rationale/{key}")
def agent_rationale(key: str):
    """Full reasoning + signals for a single exception."""
    review = workflow.get_review(key)
    if review is None:
        raise HTTPException(404, "no review for key %s" % key)
    return {"key": key, "review": review}


@app.post("/api/agent/run-all")
async def agent_run_all():
    """Batch-review every pending item (rules + Gemini if configured)."""
    reviewed = _review_pending_items(use_llm=True)
    return {"reviewed": reviewed, "count": len(reviewed)}


# ----------------------------------------------------------------------
# Financials
# ----------------------------------------------------------------------
@app.get("/api/financials/demo")
def financials_demo():
    """NOI with and without reconciliation, using current workflow decisions."""
    comparison = financials.build_comparison(
        _state["ar_results"], _state["ap_results"],
        workflow.get_workflow_status, workflow.ar_key, workflow.ap_key,
    )
    comparison["properties"] = (
        portfolio.build_summary(_properties(), _state["ar_results"], _state["ap_results"])
    )
    return comparison


# ----------------------------------------------------------------------
# Chat agent (read-only, local model)
# ----------------------------------------------------------------------
@app.get("/api/chat/status")
def chat_status():
    """Whether the local model is reachable, so the UI can say so up front."""
    live = agent_chat.available()
    return {
        "available": live,
        "model": agent_chat.OLLAMA_MODEL,
        "url": agent_chat.OLLAMA_URL,
        "hint": "" if live else (
            "Start the model with `ollama serve` then `ollama pull %s`. "
            "The rest of the app works without it." % agent_chat.OLLAMA_MODEL
        ),
    }


@app.post("/api/chat")
async def chat_endpoint(request: Request):
    """Answer a question about the reconciliation data using read-only tools."""
    body = (await request.json()) or {}

    # A fresh server has no reconciled data until /api/demo runs. Without this
    # the tools query an empty set and the model reports "no exceptions" with
    # total confidence -- confidently wrong, the worst failure mode here.
    seeded = _ensure_data()
    result = agent_chat.chat(body.get("question", ""), _chat_state())
    if seeded:
        result["note"] = "sample data loaded automatically for this question"
    return result


def _ensure_data():
    """Load the bundled sample data if nothing has been reconciled yet.

    Returns True when it had to seed.
    """
    if _state["ar_results"] or _state["ap_results"]:
        return False
    try:
        demo()
    except HTTPException:
        return False
    return True


def _chat_state():
    """The read-only slice of state the chat agent may read."""
    bundle = _bundle()
    return {
        "ar_results": _state["ar_results"],
        "ap_results": _state["ap_results"],
        "properties": _properties(),
        "portfolio": bundle["portfolio"],
        "financials": bundle["financials"],
    }


# ----------------------------------------------------------------------
# Page
# ----------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Real Estate Reconciliation Agent</title>
<style>
:root{color-scheme:light dark}
*{box-sizing:border-box}
body{margin:0;padding:24px 20px 64px;font:14px/1.6 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;background:#0f1115;color:#e6e8eb}
.wrap{max-width:1180px;margin:0 auto}
h1{font-size:22px;margin:0 0 2px;letter-spacing:-.01em}
.sub{color:#9aa3ad;margin:0 0 22px;font-size:13px}
.tabs{display:flex;gap:4px;border-bottom:1px solid #262b35;margin-bottom:18px;flex-wrap:wrap}
.tab{padding:8px 15px;border-radius:8px 8px 0 0;border:1px solid transparent;border-bottom:none;background:transparent;color:#9aa3ad;font:inherit;font-size:13px;font-weight:600;cursor:pointer}
.tab:hover{background:#1e2330}
.tab.active{background:#171a21;border-color:#262b35;color:#e6e8eb}
.tab:hover.active{background:#1e2330}
.panel{background:#171a21;border:1px solid #262b35;border-radius:10px;padding:16px 18px;margin-bottom:16px}
h3{font-size:12px;margin:0 0 10px;color:#9aa3ad;text-transform:uppercase;letter-spacing:.05em}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:620px){.grid{grid-template-columns:1fr}}
label{display:block;font-size:12px;color:#9aa3ad;margin-bottom:5px}
input[type=file]{width:100%;padding:9px;font-size:13px;color:#c9ced6;background:#0f1115;border:1px dashed #333a46;border-radius:8px}
button{font:inherit;font-weight:600;padding:9px 16px;border-radius:8px;border:0;cursor:pointer;background:#3b82f6;color:#fff;font-size:13px}
button.ghost{background:#262b35;color:#d7dbe0}
button:disabled{opacity:.5;cursor:default}
button.sm{padding:4px 10px;font-size:12px}
button.green{background:#166534;color:#86efac}
button.red{background:#991b1b;color:#fca5a5}
.actions{display:flex;gap:10px;margin-top:14px;flex-wrap:wrap}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
@media(max-width:760px){.cards{grid-template-columns:1fr 1fr}}
.card{background:#171a21;border:1px solid #262b35;border-radius:10px;padding:12px 14px}
.card .k{font-size:11px;color:#9aa3ad;text-transform:uppercase;letter-spacing:.04em}
.card .v{font-size:19px;font-weight:650;margin-top:3px}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:4px}
th,td{text-align:left;padding:7px 9px;border-bottom:1px solid #262b35;vertical-align:top}
th{color:#9aa3ad;font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.04em;white-space:nowrap}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.badge{display:inline-block;padding:2px 7px;border-radius:999px;font-size:11px;font-weight:650;white-space:nowrap}
.ok{background:#14321f;color:#6ee7a0}
.warn{background:#3a2a12;color:#fbbf24}
.bad{background:#3a1618;color:#f87171}
.auto{background:#1e2330;color:#9aa3ad}
.muted{color:#6b7280;font-size:12px}
.err{color:#f87171;margin-top:12px;font-size:13px;white-space:pre-wrap}
.hidden{display:none}
.split{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:860px){.split{grid-template-columns:1fr}}
.pos{color:#6ee7a0}
.neg{color:#f87171}
.hdr{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}
.wrap.agent-open{max-width:1660px;display:grid;grid-template-columns:minmax(0,1fr) 440px;gap:18px;align-items:start}
.wrap.agent-open>.hdr{grid-column:1 / -1}
#agent-toggle.active{background:#3b82f6;color:#fff}
#agent-dock{position:sticky;top:20px;max-height:calc(100vh - 44px);overflow-y:auto;border:1px solid #262b35;border-radius:10px;background:#171a21;padding:14px 16px}
#agent-dock .panel{border:0;background:transparent;padding:0;margin:0}
@media(max-width:1080px){
  .wrap.agent-open{grid-template-columns:1fr}
  .wrap.agent-open>#agent-dock{grid-column:1;position:static;max-height:none}
}
tr.total td{border-top:2px solid #3a4150;font-weight:650}
</style>
</head>
<body>
<div class="wrap" id="wrap">
  <div class="hdr">
    <div>
      <h1>Real Estate Reconciliation Agent</h1>
      <p class="sub">Rent roll vs receipts (AR) and vendor invoices vs approved work
         orders (AP), with review workflow and NOI reporting across the portfolio.</p>
    </div>
    <button id="agent-toggle" class="ghost" title="Ask the assistant about this data">Agent</button>
  </div>

  <div id="agent-main">
  <div class="tabs">
    <button class="tab active" data-tab="portfolio">Portfolio</button>
    <button class="tab" data-tab="ar">Rent Roll / AR</button>
    <button class="tab" data-tab="ap">Vendor Invoices / AP</button>
    <button class="tab" data-tab="workflow">Workflow Review</button>
    <button class="tab" data-tab="financials">Financials (NOI)</button>
  </div>

  <div class="panel">
    <div class="grid">
      <div>
        <label>Rent roll CSV (AR expected)</label>
        <input id="f-roll" type="file" accept=".csv,text/csv">
        <label style="margin-top:10px">Receipts CSV (AR received)</label>
        <input id="f-receipts" type="file" accept=".csv,text/csv">
        <button class="ghost sm" style="margin-top:8px" id="run-ar">Reconcile AR</button>
      </div>
      <div>
        <label>Work orders CSV (AP expected)</label>
        <input id="f-wo" type="file" accept=".csv,text/csv">
        <label style="margin-top:10px">Invoices CSV (AP received)</label>
        <input id="f-inv" type="file" accept=".csv,text/csv">
        <button class="ghost sm" style="margin-top:8px" id="run-ap">Reconcile AP</button>
      </div>
    </div>
    <div class="actions">
      <button id="demo">Run on sample data</button>
    </div>
    <div id="err" class="err hidden"></div>
  </div>

  <div id="panel-portfolio">
    <div class="cards" id="pf-cards"></div>
    <div class="panel" style="margin-top:16px">
      <h3>Portfolio Rollup</h3>
      <table>
        <thead><tr>
          <th>Property</th><th>Units</th><th>Occupancy</th>
          <th style="text-align:right">Billed</th>
          <th style="text-align:right">Collected</th>
          <th style="text-align:right">Shortfall</th>
          <th style="text-align:right">AR Exc.</th>
          <th style="text-align:right">AP Billed</th>
          <th style="text-align:right">AP Exc.</th>
          <th style="text-align:right">NOI</th>
        </tr></thead>
        <tbody id="pf-rows"></tbody>
      </table>
    </div>
    <div class="panel">
      <h3>AR Aging</h3>
      <table>
        <thead><tr>
          <th>Current</th><th>1–30</th><th>31–60</th><th>61–90</th><th>90+</th><th>Total</th>
        </tr></thead>
        <tbody id="aging-rows"></tbody>
      </table>
    </div>
  </div>

  <div id="panel-ar" class="hidden">
    <div class="cards" id="ar-cards"></div>
    <div class="panel" style="margin-top:16px">
      <h3>Rent Roll vs Receipts — Exceptions</h3>
      <table>
        <thead><tr>
          <th>Unit</th><th>Tenant</th><th>Property</th>
          <th style="text-align:right">Due</th>
          <th style="text-align:right">Received</th>
          <th style="text-align:right">Variance</th>
          <th>Late</th><th>Status</th><th>Detail</th>
        </tr></thead>
        <tbody id="ar-rows"></tbody>
      </table>
    </div>
  </div>

  <div id="panel-ap" class="hidden">
    <div class="cards" id="ap-cards"></div>
    <div class="panel" style="margin-top:16px">
      <h3>Vendor Invoices vs Approved Work Orders — Exceptions</h3>
      <table>
        <thead><tr>
          <th>Invoice</th><th>Vendor</th><th>Property</th><th>GL</th>
          <th style="text-align:right">Billed</th>
          <th style="text-align:right">Approved</th>
          <th style="text-align:right">Variance</th>
          <th>Status</th><th>Detail</th>
        </tr></thead>
        <tbody id="ap-rows"></tbody>
      </table>
    </div>
  </div>

  <div id="panel-workflow" class="hidden">
    <div class="cards" id="wf-cards"></div>
    <div class="panel" style="margin-top:16px">
      <h3>Review Queue</h3>
      <div id="wf-msg" class="muted">Run reconciliation to populate the queue.</div>
      <table id="wf-table" class="hidden">
        <thead><tr>
          <th>Side</th><th>Reference</th><th>Property</th>
          <th style="text-align:right">Amount</th>
          <th>Recon Status</th><th>Detail</th><th>Action</th>
        </tr></thead>
        <tbody id="wf-rows"></tbody>
      </table>
    </div>
  </div>

  <div id="panel-financials" class="hidden">
    <div id="fin-msg" class="panel">Run reconciliation to populate the statement.</div>
    <div id="fin-body" class="hidden">
      <div class="cards" id="fin-cards"></div>
      <div class="split" style="margin-top:16px">
        <div class="panel">
          <h3>Without Reconciliation</h3>
          <table><tbody id="fin-without"></tbody></table>
        </div>
        <div class="panel">
          <h3>With Reconciliation</h3>
          <table><tbody id="fin-with"></tbody></table>
        </div>
      </div>
      <div class="panel">
        <h3>Delta (With − Without)</h3>
        <table>
          <thead><tr><th>Account</th><th style="text-align:right">Without</th>
            <th style="text-align:right">With</th><th style="text-align:right">Delta</th></tr></thead>
          <tbody id="fin-delta"></tbody>
        </table>
      </div>
      <div class="panel">
        <h3>Balance Sheet</h3>
        <table>
          <thead><tr>
            <th>Account</th><th>Name</th>
            <th style="text-align:right">Without</th>
            <th style="text-align:right">With</th>
            <th style="text-align:right">Delta</th>
          </tr></thead>
          <tbody id="fin-bs"></tbody>
        </table>
      </div>
    </div>
  </div>
  </div><!-- /#agent-main -->

  <aside id="agent-dock" class="hidden">
      <h3>Assistant</h3>
      <div id="chat-status" class="muted">Checking whether the local model is running…</div>
      <div id="chat-log" style="margin-top:12px;max-height:52vh;overflow-y:auto"></div>
      <div style="display:flex;gap:8px;margin-top:12px">
        <input id="chat-input" type="text" placeholder="e.g. which tenants are past due and by how much?"
               style="flex:1">
        <button id="chat-send">Ask</button>
      </div>
      <div id="chat-samples" style="margin-top:10px"></div>
      <div class="muted" style="margin-top:10px">
        The assistant reads the reconciliation data through read-only tools. It cannot
        approve, reject, or change anything — and every answer shows the rows it used.
      </div>
  </aside>
</div>

<script>
const $ = id => document.getElementById(id);
const MONEY = n => (Number(n) < 0 ? '-$' : '$') + Math.abs(Number(n||0)).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const esc = s => String(s??'').replace(/[&<>"]/g,c=>({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;' }[c]));
const BS = {
  paid_in_full:'ok',vacant_no_charge:'auto',partial_payment:'warn',
  non_payment:'bad',overpayment:'warn',rent_escalation_missed:'warn',
  unmatched_receipt:'bad',matched:'ok',over_amount:'warn',
  over_budget:'bad',amount_variance:'warn',vendor_mismatch:'warn',
  no_work_order:'bad',duplicate_invoice:'bad'
};
const WS = {approved:'ok',rejected:'bad',pending:'warn',auto_approved:'auto'};
const CLEAN_AR = new Set(['paid_in_full','vacant_no_charge']);

let STATE = null;

// tabs
document.querySelectorAll('.tab').forEach(t => t.onclick = () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.toggle('active', x === t));
  ['portfolio','ar','ap','workflow','financials'].forEach(p =>
    $('panel-'+p).classList.toggle('hidden', p !== t.dataset.tab));
  if (t.dataset.tab === 'workflow') loadWorkflow();
  if (t.dataset.tab === 'financials') loadFinancials();
});

// agent dock: click the header button to split the view and dock the assistant
const AGENT_KEY = 'recon.agentOpen';
let agentOpen = false;

function setAgent(open) {
  agentOpen = open;
  $('wrap').classList.toggle('agent-open', open);
  $('agent-dock').classList.toggle('hidden', !open);
  $('agent-toggle').classList.toggle('active', open);
  try { localStorage.setItem(AGENT_KEY, open ? '1' : '0'); } catch {}
  if (open) initChat();
}

$('agent-toggle').onclick = () => setAgent(!agentOpen);

function fail(msg) {
  const b = $('err'); b.textContent = msg; b.classList.remove('hidden');
}
const clearErr = () => $('err').classList.add('hidden');

async function api(url, opts) {
  clearErr();
  const r = await fetch(url, opts);
  const text = await r.text();
  let data;
  try { data = JSON.parse(text); } catch { throw new Error(r.status+': '+text.slice(0,300)); }
  if (!r.ok) throw new Error(data.detail || (r.status+' error'));
  return data;
}

async function load(url, opts) {
  [$('demo'),$('run-ar'),$('run-ap')].forEach(b => b.disabled = true);
  try {
    STATE = await api(url, opts);
    render();
  } catch (e) { fail(e.message); }
  finally { [$('demo'),$('run-ar'),$('run-ap')].forEach(b => b.disabled = false); }
}

$('demo').onclick = () => load('/api/demo', {method:'POST'});

$('run-ar').onclick = () => {
  const a = $('f-roll').files[0], b = $('f-receipts').files[0];
  if (!a || !b) return fail('Choose both a rent roll CSV and a receipts CSV.');
  const body = new FormData();
  body.append('rent_roll', a); body.append('receipts', b);
  load('/api/reconcile/ar', {method:'POST', body});
};

$('run-ap').onclick = () => {
  const a = $('f-wo').files[0], b = $('f-inv').files[0];
  if (!a || !b) return fail('Choose both a work orders CSV and an invoices CSV.');
  const body = new FormData();
  body.append('work_orders', a); body.append('invoices', b);
  load('/api/reconcile/ap', {method:'POST', body});
};

function card(k, v) { return `<div class="card"><div class="k">${k}</div><div class="v">${v}</div></div>`; }

function render() {
  if (!STATE) return;
  const s = STATE;

  // ---- Portfolio ----
  $('pf-cards').innerHTML = [
    card('Properties', s.portfolio.properties.length),
    card('Total Units', s.portfolio.portfolio.units),
    card('Portfolio Occupancy', s.portfolio.portfolio.occupancy_rate + '%'),
    card('Portfolio NOI', MONEY(s.portfolio.portfolio.noi)),
  ].join('');

  const rows = s.portfolio.properties.concat([s.portfolio.portfolio]);
  $('pf-rows').innerHTML = rows.map(p => `
    <tr class="${p.property_id === 'PORTFOLIO' ? 'total' : ''}">
      <td>${esc(p.name)}</td>
      <td class="num">${p.units}</td>
      <td class="num">${p.occupancy_rate}%</td>
      <td class="num">${MONEY(p.billed)}</td>
      <td class="num">${MONEY(p.collected)}</td>
      <td class="num ${p.shortfall > 0 ? 'neg' : ''}">${MONEY(p.shortfall)}</td>
      <td class="num">${p.ar_exceptions}</td>
      <td class="num">${MONEY(p.ap_invoiced)}</td>
      <td class="num">${p.ap_exceptions}</td>
      <td class="num">${MONEY(p.noi)}</td>
    </tr>`).join('');

  const ag = s.aging;
  $('aging-rows').innerHTML = `<tr>
      <td class="num">${MONEY(ag.current)}</td>
      <td class="num">${MONEY(ag['1_30'])}</td>
      <td class="num">${MONEY(ag['31_60'])}</td>
      <td class="num">${MONEY(ag['61_90'])}</td>
      <td class="num">${MONEY(ag.over_90)}</td>
      <td class="num"><strong>${MONEY(ag.total)}</strong></td>
    </tr>`;

  // ---- AR ----
  const ars = s.ar.summary;
  $('ar-cards').innerHTML = [
    card('Expected Charges', ars.expected_charges),
    card('Exceptions', ars.exceptions),
    card('Collection Rate', ars.collection_rate + '%'),
    card('Outstanding', MONEY(ars.shortfall)),
  ].join('');

  $('ar-rows').innerHTML = s.ar.results
    .filter(r => !CLEAN_AR.has(r.status))
    .map(r => `
    <tr>
      <td>${esc(r.unit)}</td>
      <td>${esc(r.tenant) || '<span class="muted">—</span>'}</td>
      <td>${esc(r.property_id)}</td>
      <td class="num">${MONEY(r.amount_due)}</td>
      <td class="num">${MONEY(r.amount_received)}</td>
      <td class="num ${Number(r.variance) < 0 ? 'neg' : 'pos'}">${MONEY(r.variance)}</td>
      <td class="num">${esc(r.days_late) || '<span class="muted">—</span>'}</td>
      <td><span class="badge ${BS[r.status]||'warn'}">${esc(r.status)}</span></td>
      <td>${esc(r.detail) || '<span class="muted">—</span>'}</td>
    </tr>`).join('') || '<tr><td colspan="9" class="muted">No AR exceptions.</td></tr>';

  // ---- AP ----
  const aps = s.ap.summary;
  $('ap-cards').innerHTML = [
    card('Invoices', aps.invoices),
    card('Exceptions', aps.exceptions),
    card('Total Billed', MONEY(aps.total_billed)),
    card('Variance', MONEY(aps.variance)),
  ].join('');

  $('ap-rows').innerHTML = s.ap.results
    .filter(r => r.status !== 'matched')
    .map(r => `
    <tr>
      <td>${esc(r.invoice_number)}</td>
      <td>${esc(r.vendor)}</td>
      <td>${esc(r.property_id)}</td>
      <td>${esc(r.gl_account) || '<span class="muted">—</span>'}</td>
      <td class="num">${MONEY(r.invoice_amount)}</td>
      <td class="num">${r.approved_amount ? MONEY(r.approved_amount) : '<span class="muted">—</span>'}</td>
      <td class="num ${Number(r.variance) > 0 ? 'neg' : 'pos'}">${MONEY(r.variance)}</td>
      <td><span class="badge ${BS[r.status]||'warn'}">${esc(r.status)}</span></td>
      <td>${esc(r.detail) || '<span class="muted">—</span>'}</td>
    </tr>`).join('') || '<tr><td colspan="9" class="muted">No AP exceptions.</td></tr>';

  // ---- Workflow + financials, drawn from the same bundle so every tab is
  // populated on first paint instead of racing separate fetches ----
  drawWorkflow(s.workflow, s.pending);
  drawFinancials(s.financials);
}

function drawWorkflow(sum, pending) {
  $('wf-cards').innerHTML = [
    card('Auto-Approved', sum.auto_approved),
    card('Approved', sum.approved),
    card('Rejected', sum.rejected),
    card('Pending', sum.pending),
  ].join('');

  $('wf-msg').classList.add('hidden');
  $('wf-table').classList.remove('hidden');

  $('wf-rows').innerHTML = (pending || []).map(r => {
    const rev = r.review || {};
    const rec = rev.recommendation || '';
    const recClass = {approve:'ok', reject:'bad', escalate:'warn'}[rec] || 'auto';
    const sigRows = (rev.signals || []).map(s =>
      `<tr><td>${esc(s.factor)}</td><td>${esc(s.finding)}</td><td class="num">${s.weight}</td></tr>`).join('')
      || '<tr><td colspan="3" class="muted">no signals</td></tr>';
    return `
    <tr>
      <td><span class="badge auto">${esc(r.side)}</span></td>
      <td>${esc(r.reference)}</td>
      <td>${esc(r.property_id)}</td>
      <td class="num">${MONEY(r.amount)}</td>
      <td><span class="badge ${BS[r.recon_status]||'warn'}">${esc(r.recon_status)}</span></td>
      <td>
        ${rec ? `<span class="badge ${recClass}">${esc(rec)}</span>
                 <span class="muted">${Math.round((rev.confidence||0)*100)}%</span>
                 <div style="margin-top:2px">${esc(rev.rationale||'')}</div>` : '<span class="muted">no review</span>'}
        <button class="sm ghost" style="margin-top:4px" onclick="toggleSignals('${r.key}')">Explain</button>
      </td>
      <td>
        <button class="sm green" onclick="decide('${r.key}','approve')">✓</button>
        <button class="sm red" onclick="decide('${r.key}','reject')">✗</button>
      </td>
    </tr>
    <tr id="signals-${r.key}" class="hidden">
      <td colspan="7" style="background:#14171d">
        <table style="margin:0">
          <thead><tr><th>Factor</th><th>Finding</th><th style="text-align:right">Weight</th></tr></thead>
          <tbody>${sigRows}</tbody>
        </table>
        ${rev.next_step ? `<div class="muted" style="margin-top:6px">Next step: ${esc(rev.next_step)}</div>` : ''}
      </td>
    </tr>`;
  }).join('') || '<tr><td colspan="7" class="muted">Queue is clear.</td></tr>';
}

function toggleSignals(key) {
  const el = $('signals-' + key);
  if (el) el.classList.toggle('hidden');
}

function drawFinancials(d) {
  if (!d) return;
  $('fin-msg').classList.add('hidden');
  $('fin-body').classList.remove('hidden');

  const before = d.without_reconciliation, after = d.with_reconciliation;
  $('fin-cards').innerHTML = [
    card('NOI — No Recon', MONEY(d.delta.noi_without)),
    card('NOI — Reconciled', MONEY(d.delta.noi_with)),
    card('NOI Impact', MONEY(d.delta.noi_delta)),
    card('Held for Review', MONEY(d.delta.held_for_review)),
  ].join('');

  const stmt = (title, s) => {
    let html = '<tr><th colspan="3">Revenue</th></tr>';
    html += s.revenue.map(r => `<tr><td>${esc(r.account)}</td><td>${esc(r.name)}</td><td class="num">${MONEY(r.amount)}</td></tr>`).join('')
          || '<tr><td colspan="3" class="muted">none</td></tr>';
    html += `<tr class="total"><td colspan="2">Total Revenue</td><td class="num">${MONEY(s.total_revenue)}</td></tr>`;
    html += '<tr><th colspan="3">Operating Expenses</th></tr>';
    html += s.expenses.map(r => `<tr><td>${esc(r.account)}</td><td>${esc(r.name)}</td><td class="num">${MONEY(r.amount)}</td></tr>`).join('')
          || '<tr><td colspan="3" class="muted">none</td></tr>';
    html += `<tr class="total"><td colspan="2">Total Expenses</td><td class="num">${MONEY(s.total_expenses)}</td></tr>`;
    html += `<tr class="total"><td colspan="2">${title}</td><td class="num">${MONEY(s.noi)}</td></tr>`;
    if (s.uncollected > 0)
      html += `<tr><td colspan="2" class="muted">Uncollected → Tenant Receivables</td><td class="num muted">${MONEY(s.uncollected)}</td></tr>`;
    if (s.held_for_review > 0)
      html += `<tr><td colspan="2" class="muted">Held in AP Reserve</td><td class="num muted">${MONEY(s.held_for_review)}</td></tr>`;
    return html;
  };

  $('fin-without').innerHTML = stmt('NOI', before);
  $('fin-with').innerHTML = stmt('NOI', after);

  const deltaRows = d.delta.revenue.concat(d.delta.expenses);
  $('fin-delta').innerHTML = deltaRows.map(r => `
    <tr>
      <td>${esc(r.account)} — ${esc(r.name)}</td>
      <td class="num">${MONEY(r.without_recon)}</td>
      <td class="num">${MONEY(r.with_recon)}</td>
      <td class="num ${r.delta < 0 ? 'neg' : r.delta > 0 ? 'pos' : ''}">${MONEY(r.delta)}</td>
    </tr>`).join('');

  // Balance sheet: both views merged by account so the delta is visible.
  const index = rows => Object.fromEntries((rows || []).map(r => [r.account, r]));
  const bIdx = index(before.balance_sheet);
  const aIdx = index(after.balance_sheet);
  const accounts = [...new Set([...Object.keys(bIdx), ...Object.keys(aIdx)])].sort();
  const BS_CLASS = {Asset:'ok', Liability:'warn', Equity:'auto'};

  let bsHtml = accounts.map(acct => {
    const b = bIdx[acct] || {amount: 0};
    const a = aIdx[acct] || {amount: 0, name: b.name, type: b.type};
    const delta = (a.amount || 0) - (b.amount || 0);
    const type = a.type || b.type;
    return `<tr>
      <td>${esc(acct)}</td>
      <td>${esc(a.name || b.name)} <span class="badge ${BS_CLASS[type]||'auto'}">${esc(type)}</span></td>
      <td class="num">${MONEY(b.amount || 0)}</td>
      <td class="num">${MONEY(a.amount || 0)}</td>
      <td class="num ${delta < 0 ? 'neg' : delta > 0 ? 'pos' : ''}">${MONEY(delta)}</td>
    </tr>`;
  }).join('');

  const totalRow = (label, b, a) => `<tr class="total">
      <td colspan="2">${label}</td>
      <td class="num">${MONEY(b)}</td>
      <td class="num">${MONEY(a)}</td>
      <td class="num"></td></tr>`;

  bsHtml += totalRow('Total Assets', before.total_assets, after.total_assets);
  bsHtml += totalRow('Total Liabilities', before.total_liabilities, after.total_liabilities);
  bsHtml += totalRow('Total Equity', before.total_equity, after.total_equity);
  $('fin-bs').innerHTML = bsHtml;
}
async function loadWorkflow() {
  try {
    const [pend, sum] = await Promise.all([
      api('/api/workflow/pending'),
      api('/api/workflow/summary'),
    ]);
    $('wf-cards').innerHTML = [
      card('Auto-Approved', sum.auto_approved),
      card('Approved', sum.approved),
      card('Rejected', sum.rejected),
      card('Pending', sum.pending),
    ].join('');

    $('wf-msg').classList.add('hidden');
    $('wf-table').classList.remove('hidden');

    $('wf-rows').innerHTML = pend.pending.map(r => {
      const rev = r.review || {};
      const rec = rev.recommendation || '';
      const recClass = {approve:'ok', reject:'bad', escalate:'warn'}[rec] || 'auto';
      const sigRows = (rev.signals || []).map(s =>
        `<tr><td>${esc(s.factor)}</td><td>${esc(s.finding)}</td><td class="num">${s.weight}</td></tr>`).join('')
        || '<tr><td colspan="3" class="muted">no signals</td></tr>';
      return `
      <tr>
        <td><span class="badge auto">${esc(r.side)}</span></td>
        <td>${esc(r.reference)}</td>
        <td>${esc(r.property_id)}</td>
        <td class="num">${MONEY(r.amount)}</td>
        <td><span class="badge ${BS[r.recon_status]||'warn'}">${esc(r.recon_status)}</span></td>
        <td>
          ${rec ? `<span class="badge ${recClass}">${esc(rec)}</span>
                   <span class="muted">${Math.round((rev.confidence||0)*100)}%</span>
                   <div style="margin-top:2px">${esc(rev.rationale||'')}</div>` : '<span class="muted">no review</span>'}
          <button class="sm ghost" style="margin-top:4px" onclick="toggleSignals('${r.key}')">Explain</button>
        </td>
        <td>
          <button class="sm green" onclick="decide('${r.key}','approve')">✓</button>
          <button class="sm red" onclick="decide('${r.key}','reject')">✗</button>
        </td>
      </tr>
      <tr id="signals-${r.key}" class="hidden">
        <td colspan="7" style="background:#14171d">
          <table style="margin:0">
            <thead><tr><th>Factor</th><th>Finding</th><th style="text-align:right">Weight</th></tr></thead>
            <tbody>${sigRows}</tbody>
          </table>
          ${rev.next_step ? `<div class="muted" style="margin-top:6px">Next step: ${esc(rev.next_step)}</div>` : ''}
        </td>
      </tr>`;
    }).join('') || '<tr><td colspan="7" class="muted">Queue is clear.</td></tr>';
  } catch (e) { fail(e.message); }
}

async function decide(key, action) {
  const reviewer = prompt('Reviewer name:');
  if (!reviewer) return;
  const note = prompt(action === 'approve' ? 'Note (optional):' : 'Reason:') || '';
  try {
    await api('/api/workflow/'+encodeURIComponent(key)+'/'+action, {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({reviewer, note}),
    });
    await loadWorkflow();
  } catch (e) { fail(e.message); }
}

// ---- Financials ----
async function loadFinancials() {
  try {
    const d = await api('/api/financials/demo');
    $('fin-msg').classList.add('hidden');
    $('fin-body').classList.remove('hidden');

    const before = d.without_reconciliation, after = d.with_reconciliation;
    $('fin-cards').innerHTML = [
      card('NOI — No Recon', MONEY(d.delta.noi_without)),
      card('NOI — Reconciled', MONEY(d.delta.noi_with)),
      card('NOI Impact', MONEY(d.delta.noi_delta)),
      card('Held for Review', MONEY(d.delta.held_for_review)),
    ].join('');

    const stmt = (title, s) => {
      let html = '<tr><th colspan="3">Revenue</th></tr>';
      html += s.revenue.map(r => `<tr><td>${esc(r.account)}</td><td>${esc(r.name)}</td><td class="num">${MONEY(r.amount)}</td></tr>`).join('')
            || '<tr><td colspan="3" class="muted">none</td></tr>';
      html += `<tr class="total"><td colspan="2">Total Revenue</td><td class="num">${MONEY(s.total_revenue)}</td></tr>`;
      html += '<tr><th colspan="3">Operating Expenses</th></tr>';
      html += s.expenses.map(r => `<tr><td>${esc(r.account)}</td><td>${esc(r.name)}</td><td class="num">${MONEY(r.amount)}</td></tr>`).join('')
            || '<tr><td colspan="3" class="muted">none</td></tr>';
      html += `<tr class="total"><td colspan="2">Total Expenses</td><td class="num">${MONEY(s.total_expenses)}</td></tr>`;
      html += `<tr class="total"><td colspan="2">${title}</td><td class="num">${MONEY(s.noi)}</td></tr>`;
      if (s.uncollected > 0)
        html += `<tr><td colspan="2" class="muted">Uncollected → Tenant Receivables</td><td class="num muted">${MONEY(s.uncollected)}</td></tr>`;
      return html;
    };

    $('fin-without').innerHTML = stmt('NOI', before);
    $('fin-with').innerHTML = stmt('NOI', after);

    const deltaRows = d.delta.revenue.concat(d.delta.expenses);
    $('fin-delta').innerHTML = deltaRows.map(r => `
      <tr>
        <td>${esc(r.account)} — ${esc(r.name)}</td>
        <td class="num">${MONEY(r.without_recon)}</td>
        <td class="num">${MONEY(r.with_recon)}</td>
        <td class="num ${r.delta < 0 ? 'neg' : r.delta > 0 ? 'pos' : ''}">${MONEY(r.delta)}</td>
      </tr>`).join('');
  } catch (e) { fail(e.message); }
}

// ---- Assistant ----
let chatReady = false;

const SAMPLE_QUESTIONS = [
  'Which tenants are past due, and by how much?',
  'Are there any duplicate vendor invoices?',
  'Which vendor has the most exceptions?',
  'What is NOI with and without reconciliation?',
  'Is INV-9002 actually a duplicate or a re-bill?',
];

async function initChat() {
  if (chatReady) return;
  chatReady = true;

  $('chat-samples').innerHTML = SAMPLE_QUESTIONS.map(q =>
    `<button class="sm ghost" style="margin:0 6px 6px 0"
       onclick="askSample(this)">${esc(q)}</button>`).join('');

  try {
    const s = await api('/api/chat/status');
    $('chat-status').innerHTML = s.available
      ? `<span class="badge ok">ready</span> local model <code>${esc(s.model)}</code>`
      : `<span class="badge warn">offline</span> ${esc(s.hint)}`;
  } catch (e) {
    $('chat-status').textContent = 'Could not reach the assistant endpoint.';
  }
}

function askSample(btn) {
  $('chat-input').value = btn.textContent;
  sendChat();
}

function addBubble(kind, html) {
  const wrap = document.createElement('div');
  wrap.style.margin = '10px 0';
  wrap.style.padding = '10px 12px';
  wrap.style.borderRadius = '8px';
  wrap.style.border = '1px solid #262b35';
  wrap.style.background = kind === 'user' ? '#1b2130' : '#171a21';
  wrap.innerHTML = html;
  $('chat-log').appendChild(wrap);
  $('chat-log').scrollTop = $('chat-log').scrollHeight;
  return wrap;
}

async function sendChat() {
  const question = $('chat-input').value.trim();
  if (!question) return;

  $('chat-input').value = '';
  addBubble('user', esc(question));
  const pending = addBubble('agent', '<span class="muted">thinking…</span>');
  $('chat-send').disabled = true;

  try {
    const res = await api('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({question}),
    });

    let html = `<div>${esc(res.answer).replace(/\\n/g, '<br>')}</div>`;

    // Show which rows the answer was built from, so it can be traced.
    if (res.tools_used && res.tools_used.length) {
      const calls = res.citations || [];
      const detail = calls.map(c => {
        const args = Object.entries(c.arguments || {})
          .map(([k, v]) => `${k}=${v}`).join(', ');
        const got = c.count === undefined || c.count === null ? '' : ` → ${c.count} rows`;
        return `<code>${esc(c.tool)}(${esc(args)})</code>${got}`;
      }).join('<br>');
      html += `<div class="muted" style="margin-top:8px">
        <strong>Sources</strong> (${res.steps} step${res.steps === 1 ? '' : 's'})<br>${detail}
      </div>`;
    }
    if (res.source === 'unavailable') {
      html += `<div class="muted" style="margin-top:6px">No data was read — this is a
        connection message, not an answer.</div>`;
    }

    pending.innerHTML = html;
  } catch (e) {
    pending.innerHTML = `<span class="neg">${esc(e.message)}</span>`;
  } finally {
    $('chat-send').disabled = false;
    $('chat-log').scrollTop = $('chat-log').scrollHeight;
  }
}

$('chat-send').onclick = sendChat;
$('chat-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') sendChat();
});

// Populate the page on first paint so the demo does not open on empty tables.
load('/api/demo', {method:'POST'}).then(() => {
  loadWorkflow();
  loadFinancials();
});

// Restore the dock if it was left open. Closed by default: full width until asked.
try {
  if (localStorage.getItem(AGENT_KEY) === '1') setAgent(true);
} catch {}
</script>
</body>
</html>
"""
