# Real Estate Reconciliation Agent

Reconciles a rent roll against tenant receipts (AR) and vendor invoices against
approved work orders (AP), routes exceptions through a review workflow, and
reports NOI before and after reconciliation across a property portfolio.

Built as a hackathon MVP. The scope is deliberately narrow: CSV in, verdicts
out. No OCR, no database, no auth.

## What it catches

**AR — rent roll vs receipts** (`reconcile_ar.py`)

| Status | Meaning |
| --- | --- |
| `paid_in_full` | Received matches the amount due within tolerance (default 2%) |
| `partial_payment` | Short pay beyond tolerance |
| `non_payment` | Rent is due and no receipt was recorded |
| `overpayment` | Received exceeds the amount due beyond tolerance |
| `rent_escalation_missed` | Base rent paid but CAM/other charges never collected |
| `unmatched_receipt` | Payment posted to a unit not on the rent roll |
| `vacant_unit_payment` | Payment against a unit marked vacant |
| `vacant_no_charge` | Unit vacant, nothing expected — not an exception |

Escalation is checked *before* the tolerance band. CAM is small relative to
rent, so a tenant paying base rent only would otherwise fall inside tolerance
and read as paid in full.

**AP — invoices vs approved work orders** (`reconcile.py`)

| Status | Meaning |
| --- | --- |
| `matched` | Work order found, billed amount within tolerance, vendor resolves to the approved vendor |
| `over_budget` | Billed above the approved amount |
| `amount_variance` | Billed below the approved amount |
| `vendor_mismatch` | Work order found but billed by a different entity |
| `no_work_order` | No usable work order reference, or not in the file |
| `duplicate_invoice` | This invoice number was already processed |

Vendor names match across casing, stray whitespace, punctuation and legal
suffixes — `Bright Spark Electric`, `BRIGHT SPARK ELECTRIC` and
`Bright Spark Electric Inc.` are the same entity, and are not a mismatch.

## Run it

### Locally

```bash
pip install -r requirements.txt
python data/generate_data.py     # writes the sample CSVs
uvicorn app:app --port 8000
```

Open <http://localhost:8000> and click **Run on sample data**, or upload your own
CSVs per side.

### Docker

```bash
docker build -t real-estate-recon-agent .
docker run --rm -p 8000:8000 real-estate-recon-agent
```

The sample dataset is generated during the build, so `/api/demo` works with no
setup. There is a `HEALTHCHECK` on `/health`.

## Command line

```bash
python reconcile_ar.py --rent-roll data/rent_roll.csv --receipts data/ar_receipts.csv
python reconcile.py --work-orders data/ap_work_orders.csv --invoices data/ap_invoices.csv
```

`--tolerance` tightens the acceptable drift on either engine.

## Verify it works

Two suites, both exiting non-zero on failure.

**Classification accuracy** — regenerates the data, reconciles both sides, and
scores against ground truth:

```bash
python run_all.py
```

```
=== 4/5 score both suites ===
Score: AR (rent roll vs receipts)
accuracy               39/39 (100.0%)
Score: AP (invoices vs work orders)
accuracy               35/35 (100.0%)
```

AR is scored by key (`lease_id`, or `receipt_id` for stray receipts) because the
reconciler emits rent-roll order then strays, which does not line up positionally
with ground truth. AP is scored positionally: both sides are built in invoice-file
order with duplicates appended in the same order.

It also checks that a malformed cell raises `DataError` rather than `SystemExit`
on *both* engines — the latter would tear down a request worker instead of
returning a 400.

**HTTP surface** — 51 checks against a server already running on port 8000,
covering the page and every endpoint, including two kinds of bad upload (missing
column, non-numeric cell) and that the server survives them:

```bash
uvicorn app:app --port 8000 &
python test_api.py
```

## API

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Liveness probe |
| `POST /api/demo` | Reconcile bundled rent roll, receipts, work orders and invoices |
| `POST /api/reconcile/ar` | Multipart: `rent_roll`, `receipts` |
| `POST /api/reconcile/ap` | Multipart: `work_orders`, `invoices` |
| `GET /api/ar/results` | AR summary + rows |
| `GET /api/ap/results` | AP summary + rows |
| `GET /api/portfolio/summary` | Per-property rollup, portfolio total, AR aging |
| `GET /api/workflow/summary` | Review queue counts |
| `GET /api/workflow/pending` | Exceptions awaiting a decision, tagged `AR` / `AP` |
| `POST /api/workflow/<key>/approve` | Body: `{reviewer, note}` |
| `POST /api/workflow/<key>/reject` | Body: `{reviewer, note}` |
| `GET /api/financials/demo` | NOI statement, without vs with reconciliation |

Workflow keys are namespaced — `AR-<lease_id>` for rent-roll exceptions,
`AR-<receipt_id>` for stray receipts, `AP-<invoice_number>` for invoices — so
both sides share one review queue without colliding.

```bash
curl -X POST "http://localhost:8000/api/demo"
```

## Sample data

`data/generate_data.py` is seeded, so it produces identical data every run.

Three properties, 38 units, March 2026:

| File | Rows | Contents |
| --- | --- | --- |
| `properties.csv` | 3 | Maple Ridge (24 units), Cedar Point (8), Elm Street Lofts (6) |
| `rent_roll.csv` | 38 | One row per unit; vacant units carry no charge |
| `ar_receipts.csv` | 31 | Tenant payments (ABSENT rows for non-payment) |
| `ap_work_orders.csv` | 32 | Approved vendor spend, GL-coded |
| `ap_invoices.csv` | 35 | Vendor invoices, including 3 duplicates |
| `ground_truth_ar.csv` | 39 | Correct status per charge and per stray receipt |
| `ground_truth_ap.csv` | 35 | Correct status per invoice |

Operating expense is sized at ~38% of rent billed (`OPEX_RATIO`), the industry
norm for this asset class. The generator asserts the realised ratio lands in a
25–55% band: drawing vendor spend independently of revenue once produced $77k of
spend against $50k of monthly rent, which made NOI deeply negative and read as a
broken demo rather than a finding.

Defect counts are derived from the number of occupied leases and work orders,
with assertions that the totals agree — a hardcoded list silently truncated under
`zip()` in an earlier revision and left purchase orders with no invoice at all.

Injections: 2 non-payments, 3 partial pays, 1 overpayment, 1 missed escalation,
1 stray receipt, 6 vacancies; 4 over-budget bills, 2 under-billed, 2 with no work
order, 3 duplicates.

## GL mapping

| Account | Name | Type |
| --- | --- | --- |
| 1100 | Cash | Asset |
| 1200 | Tenant Receivables | Asset |
| 2000 | Accounts Payable | Liability |
| 2999 | AP Reserve — Held | Liability |
| 3000 | Retained Earnings | Equity |
| 4100 | Rental Income | Revenue |
| 4200 | CAM & Other Income | Revenue |
| 5100 | Repairs & Maintenance | Expense |
| 5200 | Utilities | Expense |
| 5300 | Property Management Fee | Expense |
| 5400 | Property Tax & Insurance | Expense |

**NOI** = total revenue − total operating expense. **Assets = Liabilities +
Equity** holds in both views, with equity derived as the plug; `financials.py`
raises if it does not balance rather than shipping a statement that is silently
wrong. An earlier revision set Cash to NOI — a flow used as a balance, with no
equity line — which left assets at $4,543 against $72,488 of liabilities.

## Layout

```
app.py                     FastAPI service + five-tab UI
agent.py                   review advisor (rules + optional Gemini)
reconcile_ar.py            rent roll vs receipts (AR engine)
reconcile.py               invoices vs work orders (AP engine)
recon_common.py            shared parsing, rounding, vendor normalisation
portfolio.py               per-property rollup and AR aging
workflow.py                unified AR + AP review queue + advisor reviews
financials.py              NOI statement, without vs with reconciliation
score.py                   classification accuracy against ground truth
run_all.py                 offline test entry point
test_api.py                HTTP checks against a running server
test_tools.py              offline tests for the assistant's tool layer
tools.py                   read-only queries the assistant may call
agent_chat.py              chat agent (local Llama via Ollama)
check_env.py               reports whether runtime dependencies import
Dockerfile                 container image
docker-compose.yml         app + local Llama, one command
setup_ollama.sh            pulls the model into the container
Makefile                   make test / make test-api / make run / make docker
data/generate_data.py      seeded synthetic data + ground truth
```

## The chat assistant

The **Assistant** tab answers plain-English questions about the reconciliation
data — "which tenants are past due?", "is INV-9002 actually a duplicate?",
"what is NOI with and without reconciliation?". It is backed by a local Llama
via [Ollama](https://ollama.com): no API key, no cost, and no invoice data
leaves the machine.

The model does not guess. It calls **read-only tools** (`tools.py`) that query
the same results every tab renders:

| Tool | Answers questions about |
| --- | --- |
| `list_ar_exceptions` | Tenants owing money, filterable by status, property, amount |
| `list_ap_exceptions` | Vendor overbilling, duplicates, unsupported invoices |
| `get_tenant_history` | One tenant or unit, clean rows included |
| `get_vendor_history` | One vendor's full billing pattern |
| `get_portfolio_summary` | Per-property occupancy, collections, NOI |
| `get_financials` | The NOI statement and balance sheet |

Every answer shows the tools it called and how many rows each returned, so a
figure can be traced back to the ledger. The assistant is **read-only** — it
cannot approve, reject or write; if asked to, it says the reviewer must do it
in the Workflow Review tab.

### Running it

```bash
# native
ollama serve
ollama pull llama3.1:8b
uvicorn app:app --port 8000

# or everything in containers
docker compose up -d
./setup_ollama.sh
```

`llama3.1:8b` wants roughly 8GB of RAM free. On a smaller machine use
`llama3.2:3b` and set `OLLAMA_MODEL` accordingly.

### When the model is not running

The Assistant tab reports the model as offline and tells you how to start it.
Every other feature — reconciliation, the review queue, the advisor, the
financials — is unaffected. This is deliberate: a network or model failure must
never take the app down mid-demo. Verified in `test_tools.py`, which exercises
the unavailable path and asserts the agent does not fabricate data when it
cannot reach the model.

### Providers are swappable

The tool layer is provider-agnostic; `agent_chat.py` speaks Ollama's
OpenAI-compatible tool-calling API. Pointing it at a hosted model instead is a
change to one function. The rules advisor in `agent.py` is a separate, fully
deterministic surface and stays free of any model dependency.

## The review advisor

Each pending exception in the Workflow Review tab carries an advisor verdict:
a recommendation (`approve` / `reject` / `escalate`) with a confidence, a
one-line rationale, and an **Explain** toggle that expands to the full signal
table — factor, finding, and the signed weight behind it.

Two layers, always safe:

1. **Rules (always on).** A deterministic cascade, no network, no key. The
   policy is the table at the top of `agent.py`:

   - **Materiality** — an amount above `ESCALATE_ABOVE` ($2,000) escalates,
     decisive, whatever the exception type.
   - **Control failures** — duplicate invoice, no work order, unmatched receipt,
     vacant-unit payment → reject and request a correction.
   - **Waivable variances** — a short-pay or over-budget within 10% of
     due/approved → approve as low-risk.
   - **Repeat offenders** — tenants/vendors with prior open exceptions lean
     harder toward escalation.
   - **Days late** — past-grace AR leans toward escalation.

2. **Gemini (optional enrichment).** With `GOOGLE_API_KEY` set in `.env`, the
   rationale is rewritten into clearer prose by `gemini-2.0-flash` (free tier).
   Any timeout or error falls back to the rules text and the verdict is tagged
   `rules (gemini unavailable)` — a network blip can never empty the queue.

The advisor **never acts** — it advises and the reviewer still clicks accept /
override. It is presented as judgement aid, not correctness: the reconcilers'
100% classification accuracy against ground truth is the number that measures
the system.

Endpoints: `POST /api/agent/review/<key>`, `GET /api/agent/rationale/<key>`,
`POST /api/agent/run-all`.

## Sample questions this answers

- Which tenants are behind, by how much, and how aged is it?
- Which vendor bills exceed what was approved, and by how much?
- What is NOI if the ledger is trusted on face value, versus after review?
- How much cash is sitting in the review reserve rather than posted?

## Not in the MVP

No PDF/OCR ingestion, no persistence (session state clears on restart), no auth,
no CAM true-up, no GAAP straight-line rent schedules, no delinquency notices.
Aging buckets are computed but there is no dunning workflow behind them.
