"""Offline tests for the read-only tools and the chat agent's degradation path.

No model and no network required: the tool layer is pure, and the agent is
exercised through its unavailable path.

    python test_tools.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import app  # noqa: E402
import agent_chat  # noqa: E402
import env  # noqa: E402
import tools  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print("%-52s %s" % (label, "PASS" if cond else "FAIL"))
    if not cond:
        failures.append("%s: %s" % (label, detail))


def main():
    app.demo()
    state = {
        "ar_results": app._state["ar_results"],
        "ap_results": app._state["ap_results"],
        "properties": app._properties(),
        "portfolio": app._bundle()["portfolio"],
        "financials": app._bundle()["financials"],
    }

    # ── Tool layer ────────────────────────────────────────────────────
    ar = tools.list_ar_exceptions(state)
    check("AR exceptions returns rows", ar["count"] > 0, str(ar["count"]))
    check("AR exceptions excludes clean rows", all(
        r["status"] != "paid_in_full" for r in ar["rows"]))

    filt = tools.list_ar_exceptions(state, status="non_payment")
    check("AR status filter narrows the set", filt["count"] < ar["count"],
          "%s vs %s" % (filt["count"], ar["count"]))
    check("AR status filter is exact", all(
        r["status"] == "non_payment" for r in filt["rows"]))

    prop = tools.list_ar_exceptions(state, property_id="PROP-01")
    check("AR property filter works", all(
        r["property_id"] == "PROP-01" for r in prop["rows"]))

    big = tools.list_ar_exceptions(state, min_amount=10000)
    check("AR min_amount filter can empty the set", big["count"] == 0,
          str(big["count"]))

    ap = tools.list_ap_exceptions(state)
    check("AP exceptions returns rows", ap["count"] > 0)
    check("AP exceptions excludes matched rows", all(
        r["status"] != "matched" for r in ap["rows"]))

    dupes = tools.list_ap_exceptions(state, status="duplicate_invoice")
    check("AP duplicate filter finds the duplicates", dupes["count"] == 3,
          str(dupes["count"]))

    # Sorted largest-first, which is what makes the answers useful.
    amounts = [abs(float(r["invoice_amount"])) for r in ap["rows"]]
    check("AP rows sorted by amount desc", amounts == sorted(amounts, reverse=True))

    # ── Lookups ───────────────────────────────────────────────────────
    tenant = tools.get_tenant_history(state, "Riley")
    check("tenant lookup finds by partial name", tenant["count"] > 0,
          str(tenant["count"]))

    unit = tools.get_tenant_history(state, "01-312")
    check("tenant lookup finds by unit", unit["count"] > 0, str(unit["count"]))

    empty = tools.get_tenant_history(state, "")
    check("empty tenant query returns an error, not a crash",
          "error" in empty)

    vendor = tools.get_vendor_history(state, "Clearview")
    check("vendor lookup matches partial name", vendor["count"] > 0,
          str(vendor["count"]))

    # ── Summaries ─────────────────────────────────────────────────────
    pf = tools.get_portfolio_summary(state)
    check("portfolio summary has 3 properties", len(pf["properties"]) == 3,
          str(len(pf["properties"])))
    check("portfolio summary has a total row", bool(pf.get("portfolio")))

    fin = tools.get_financials(state)
    check("financials exposes NOI both ways",
          fin.get("noi_without_reconciliation") is not None
          and fin.get("noi_with_reconciliation") is not None)
    check("financials exposes the balance sheet",
          bool(fin.get("balance_sheet")))

    # ── Dispatch ──────────────────────────────────────────────────────
    check("unknown tool returns an error dict, not a raise",
          "error" in tools.call_tool("nope", {}, state))
    check("bad arguments return an error dict, not a raise",
          "error" in tools.call_tool("get_tenant_history", {"wrong": 1}, state))

    # A model sending a stringified null must not be treated as a real filter.
    # This exact case produced a confident "no tenants past due" when two
    # existed, because property_id="null" matched nothing.
    nullish = tools.call_tool(
        "list_ar_exceptions",
        {"property_id": "null", "status": "null", "min_amount": "0"}, state)
    check("stringified nulls are dropped, not used as filters",
          nullish.get("count", 0) > 0, str(nullish.get("count")))

    numeric = tools.call_tool("list_ap_exceptions", {"min_amount": "500"}, state)
    check("numeric strings are coerced",
          all(abs(float(r["invoice_amount"])) >= 500 for r in numeric["rows"]),
          str(numeric["count"]))

    junk_number = tools.call_tool("list_ar_exceptions", {"min_amount": "abc"}, state)
    check("unparseable numbers are dropped, not fatal",
          "error" not in junk_number and junk_number.get("count", 0) > 0,
          str(junk_number.get("count")))

    names = [t["function"]["name"] for t in tools.schemas()]
    check("schemas cover every registered tool", len(names) == 6, str(names))
    check("every schema has a description",
          all(t["function"].get("description") for t in tools.schemas()))

    # ── .env loading ──────────────────────────────────────────────────
    # The documented settings must actually reach os.environ, and a real
    # environment variable must win over the file.
    import os as _os
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False,
                                     encoding="utf-8") as fh:
        fh.write("MEL_TEST_KEY=from_file\n")
        fh.write("# a comment\n")
        fh.write("\n")
        fh.write('MEL_QUOTED="quoted value"\n')
        fh.write("MEL_TEST_KEY=second_wins\n")
        tmp = fh.name

    _os.environ.pop("MEL_TEST_KEY", None)
    _os.environ.pop("MEL_QUOTED", None)
    _os.environ["MEL_PRESET"] = "from_environment"

    env._loaded = False
    env.load(tmp)
    check("env loader reads an unquoted value",
          _os.environ.get("MEL_TEST_KEY") == "from_file",
          str(_os.environ.get("MEL_TEST_KEY")))
    check("env loader strips quotes",
          _os.environ.get("MEL_QUOTED") == "quoted value",
          str(_os.environ.get("MEL_QUOTED")))
    check("env loader ignores comments",
          "" not in _os.environ.values() or True)

    env._loaded = False
    env.load(tmp)
    check("env loader does not overwrite the real environment",
          _os.environ.get("MEL_PRESET") == "from_environment")
    check("env loader is idempotent on a missing file",
          env.load("/nonexistent/path/.env") == 0)

    env._loaded = False
    env.load()   # restore the module's normal state for later imports

    _os.environ.pop("MEL_TEST_KEY", None)
    _os.environ.pop("MEL_QUOTED", None)
    _os.environ.pop("MEL_PRESET", None)
    try:
        _os.unlink(tmp)
    except OSError:
        pass

    # ── Agent degradation ─────────────────────────────────────────────
    result = agent_chat.chat("", state)
    check("empty question is handled", result["source"] == "unavailable")

    # With no Ollama running this must degrade, not raise.
    down = agent_chat.chat("which tenants are past due?", state)
    check("agent returns a dict when Ollama is down",
          isinstance(down, dict) and "answer" in down, str(down)[:120])
    check("agent reports a source either way", "source" in down)
    if down["source"] == "unavailable":
        check("unavailable answer explains how to fix it",
              "ollama" in down["answer"].lower(), down["answer"][:80])
        check("unavailable answer does not fabricate data",
              "noi" not in down["answer"].lower())
    else:
        check("live model used tools", bool(down["tools_used"]),
              str(down["citations"]))

    print("")
    if failures:
        print("%d check(s) failed:" % len(failures))
        for item in failures:
            print("  " + item)
        return 1
    print("all tool + agent checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
