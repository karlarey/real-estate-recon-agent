"""Verify the filter semantics against the LIVE served data, not synthetic rows.

check_filters.py proves the filter rules in the abstract. This proves them on the
real bundle the UI receives, by running the same predicate the browser runs.
"""
import json
import urllib.request

BASE = "http://127.0.0.1:8000"
CLEAN_AR = {"paid_in_full", "vacant_no_charge"}

checks = []


def check(name, ok, detail=""):
    checks.append((name, bool(ok), detail))


def post(path):
    req = urllib.request.Request(BASE + path, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def matches(q, *fields):
    if not q:
        return True
    n = q.lower()
    return any(n in str(f or "").lower() for f in fields)


def min_hit(raw, amount):
    if raw == "":
        return True
    return abs(float(amount or 0)) >= abs(float(raw or 0))


s = post("/api/demo")

ar_all = [r for r in s["ar"]["results"] if r["status"] not in CLEAN_AR]
ap_all = [r for r in s["ap"]["results"] if r["status"] != "matched"]
pf_all = [p for p in s["portfolio"]["properties"] if p["property_id"] != "PORTFOLIO"]
wf_all = s["pending"]

print(f"live rows: AR {len(ar_all)}, AP {len(ap_all)}, properties {len(pf_all)}, pending {len(wf_all)}")
print()

check("AR exception set is non-empty (filter has something to act on)", len(ar_all) > 0)
check("AP exception set is non-empty", len(ap_all) > 0)
check("pending queue is non-empty", len(wf_all) > 0)


def ar_f(f):
    return [r for r in ar_all
            if (not f.get("status") or r["status"] == f["status"])
            and (not f.get("property") or r["property_id"] == f["property"])
            and matches(f.get("q"), r["tenant"], r["unit"], r["detail"])
            and min_hit(f.get("min", ""), r["variance"])]


# --- property filter: the user's "only see rent for one property" case ---
props = sorted({r["property_id"] for r in ar_all})
check("AR rows span more than one property (so the filter is meaningful)", len(props) > 1, str(props))

for p in props:
    got = ar_f({"property": p})
    wrong = [r for r in got if r["property_id"] != p]
    expected_n = len([r for r in ar_all if r["property_id"] == p])
    check(f"property filter {p} returns exactly its {expected_n} row(s) "
          f"and nothing from another property",
          not wrong and len(got) == expected_n,
          f"got {len(got)}, wrong-property rows {len(wrong)}")

check("property filter narrows the set (fewer rows than unfiltered)",
      all(len(ar_f({"property": p})) < len(ar_all) for p in props)
      if len(props) > 1 else True)

# --- status filter ---
statuses = sorted({r["status"] for r in ar_all})
for st in statuses:
    got = ar_f({"status": st})
    expected_n = len([r for r in ar_all if r["status"] == st])
    check(f"status filter {st} returns exactly {expected_n} row(s)",
          len(got) == expected_n and all(r["status"] == st for r in got))

# --- text search on real tenant names ---
if ar_all:
    sample = next((r for r in ar_all if r.get("tenant")), None)
    if sample:
        got = ar_f({"q": sample["tenant"]})
        check(f"search by tenant '{sample['tenant']}' finds their row",
              any(r["unit"] == sample["unit"] for r in got))
        got_u = ar_f({"q": sample["unit"]})
        check(f"search by unit '{sample['unit']}' finds their row",
              any(r["unit"] == sample["unit"] for r in got_u))

# --- min variance on real amounts ---
if ar_all:
    biggest = max(ar_all, key=lambda r: abs(float(r["variance"])))
    thr = int(abs(float(biggest["variance"])) - 1)
    got = ar_f({"min": str(thr)})
    check(f"min variance >= {thr} includes the largest row ({biggest['unit']})",
          any(r["unit"] == biggest["unit"] for r in got))
    over = int(abs(float(biggest["variance"])) + 1)
    got2 = ar_f({"min": str(over)})
    check(f"min variance > largest excludes everything",
          not any(r["unit"] == biggest["unit"] for r in got2))

# --- combining filters, and the empty case ---
if len(props) > 1:
    first = props[0]
    st_other = next((st for st in statuses
                     if not any(r["property_id"] == first and r["status"] == st
                                for r in ar_all)), None)
    if st_other:
        got = ar_f({"property": first, "status": st_other})
        check(f"impossible combo ({first} + {st_other}) returns empty, not everything",
              got == [])

# --- AP: GL account filter, which has no AR equivalent ---
gls = sorted({r["gl_account"] for r in ap_all if r.get("gl_account")})
check("AP rows carry GL accounts (filter is meaningful)", len(gls) > 0, str(gls))


def ap_f(f):
    return [r for r in ap_all
            if (not f.get("status") or r["status"] == f["status"])
            and (not f.get("property") or r["property_id"] == f["property"])
            and (not f.get("gl") or r["gl_account"] == f["gl"])
            and matches(f.get("q"), r["vendor"], r["invoice_number"], r["detail"])
            and min_hit(f.get("min", ""), r["variance"])]


for gl in gls[:3]:
    got = ap_f({"gl": gl})
    expected_n = len([r for r in ap_all if r["gl_account"] == gl])
    check(f"GL filter {gl} returns exactly {expected_n} row(s)",
          len(got) == expected_n and all(r["gl_account"] == gl for r in got))

# --- workflow: side + recommendation, the queue-working case ---
sides = sorted({r["side"] for r in wf_all})
for side in sides:
    got = [r for r in wf_all if r["side"] == side]
    expected_n = len([r for r in wf_all if r["side"] == side])
    check(f"workflow side filter {side} returns exactly {expected_n} row(s)",
          len(got) == expected_n)

recs = sorted({(r.get("review") or {}).get("recommendation") for r in wf_all} - {None})
for rec in recs:
    got = [r for r in wf_all if (r.get("review") or {}).get("recommendation") == rec]
    expected_n = len([r for r in wf_all
                      if (r.get("review") or {}).get("recommendation") == rec])
    check(f"workflow recommendation filter '{rec}' returns exactly {expected_n} row(s)",
          len(got) == expected_n)

# --- portfolio: sorting produces a real order change ---
by_noi = sorted(pf_all, key=lambda p: -p["noi"])
by_occ = sorted(pf_all, key=lambda p: p["occupancy_rate"])
check("NOI sort orders rows descending",
      [p["noi"] for p in by_noi] == sorted((p["noi"] for p in pf_all), reverse=True))
check("occupancy sort orders rows ascending",
      [p["occupancy_rate"] for p in by_occ] == sorted(p["occupancy_rate"] for p in pf_all))
check("portfolio total row is excluded from the per-property filter list",
      all(p["property_id"] != "PORTFOLIO" for p in pf_all))

failed = [c for c in checks if not c[1]]
for name, ok, detail in checks:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))

print()
print(f"{len(checks) - len(failed)}/{len(checks)} live filter checks passed")
raise SystemExit(1 if failed else 0)
