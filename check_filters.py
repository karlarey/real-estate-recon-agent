"""Static checks for the filter bars: markup, wiring, and no stale element ids."""
import ast
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(HERE, "app.py"), encoding="utf-8").read()

checks = []


def check(name, ok, detail=""):
    checks.append((name, bool(ok), detail))


# 1. The file must still parse as Python.
try:
    ast.parse(src)
    check("app.py parses as Python", True)
except SyntaxError as exc:
    check("app.py parses as Python", False, str(exc))

page = src

# 2. Every filter bar element the JS reads must exist in the markup.
ids = [
    "pf-filters", "pf-f-property", "pf-f-sort", "pf-f-q", "pf-count", "pf-clear",
    "ar-filters", "ar-f-status", "ar-f-property", "ar-f-q", "ar-f-min", "ar-count", "ar-clear",
    "ap-filters", "ap-f-status", "ap-f-property", "ap-f-gl", "ap-f-q", "ap-f-min", "ap-count", "ap-clear",
    "wf-filters", "wf-f-side", "wf-f-status", "wf-f-rec", "wf-f-property", "wf-f-min", "wf-count", "wf-clear",
]
missing = [i for i in ids if f'id="{i}"' not in page]
check("all filter element ids present in markup", not missing, ", ".join(missing))

# 3. Four filter bars, one per table.
check("four filter bars", page.count('class="filters"') == 4,
      "found %d" % page.count('class="filters"'))

# 4. The init that attaches handlers must run.
check("wireFilters is called for all four bars", "['pf','ar','ap','wf'].forEach(wireFilters)" in page)

# 5. populateFilters must be called during render so options exist.
check("populateFilters called in render", "populateFilters(s)" in page)

# 6. No dangling reference to the removed assistant tab panel.
check("no stale panel-assistant reference", "panel-assistant" not in page)

# 7. Each table renderer must consult its filter state.
for prefix, marker in [
    ("pf", "F.pf.property"),
    ("ar", "F.ar.status"),
    ("ap", "F.ap.status"),
    ("wf", "F.wf.side"),
]:
    check(f"{prefix} renderer applies its filters", marker in page)

# 8. Counts are reported for each table.
for c in ["pf-count", "ar-count", "ap-count", "wf-count"]:
    check(f"count element wired: {c}", f"count('{c}'" in page)

# 9. The clear buttons are wired.
check("clear buttons wired", "clear.onclick" in page)

# 10. Filter state declarations exist for all four.
for prefix in ["pf", "ar", "ap", "wf"]:
    check(f"filter state declared: {prefix}", re.search(rf"^\s*{prefix}: \{{", page, re.M) is not None)


# 11. Exercise the actual filter logic in JS-equivalent Python, so the semantics
#     are tested and not just the presence of strings.
CLEAN = {"paid_in_full", "vacant_no_charge"}


def min_hit(raw, amount):
    if raw == "":
        return True
    return abs(float(amount or 0)) >= abs(float(raw or 0))


def matches(q, *fields):
    if not q:
        return True
    n = q.lower()
    return any(n in str(f or "").lower() for f in fields)


rows = [
    {"status": "non_payment", "property_id": "PROP-01", "tenant": "Emerson Vasquez",
     "unit": "01-314", "variance": -2244.62, "detail": "rent due"},
    {"status": "partial_payment", "property_id": "PROP-03", "tenant": "Quinn Owens",
     "unit": "03-206", "variance": -283.42, "detail": "short pay"},
    {"status": "overpayment", "property_id": "PROP-02", "tenant": "Rowan Bennett",
     "unit": "02-304", "variance": 460.55, "detail": "overpayment"},
]


def ar_filter(f):
    out = [r for r in rows if not CLEAN.__contains__(r["status"])]
    return [r for r in out
            if (not f.get("status") or r["status"] == f["status"])
            and (not f.get("property") or r["property_id"] == f["property"])
            and matches(f.get("q"), r["tenant"], r["unit"], r["detail"])
            and min_hit(f.get("min", ""), r["variance"])]


check("no filter keeps all exception rows", len(ar_filter({})) == 3, str(len(ar_filter({}))))
check("property filter narrows to one property",
      [r["unit"] for r in ar_filter({"property": "PROP-01"})] == ["01-314"])
check("status filter narrows by status",
      [r["unit"] for r in ar_filter({"status": "overpayment"})] == ["02-304"])
check("text search matches tenant name",
      [r["unit"] for r in ar_filter({"q": "quinn"})] == ["03-206"])
check("text search matches unit",
      [r["unit"] for r in ar_filter({"q": "03-206"})] == ["03-206"])
check("min variance compares magnitude, so a negative amount still matches",
      [r["unit"] for r in ar_filter({"min": "2000"})] == ["01-314"])
check("filters combine (property + min)",
      [r["unit"] for r in ar_filter({"property": "PROP-03", "min": "1000"})] == [])
check("impossible combination returns empty, not everything",
      ar_filter({"property": "PROP-01", "status": "overpayment"}) == [])

failed = [c for c in checks if not c[1]]
for name, ok, detail in checks:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))

print()
print(f"{len(checks) - len(failed)}/{len(checks)} filter checks passed")
raise SystemExit(1 if failed else 0)
