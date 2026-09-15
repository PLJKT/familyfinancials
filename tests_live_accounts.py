"""Move the live database onto the accounts model and verify the result.

Waits for the new build, takes a snapshot, runs the migration (dry run first),
then checks the arithmetic on the real data. Run:

  FF_ADMIN_PASSWORD=... python tests_live_accounts.py            # dry run + verify only
  FF_ADMIN_PASSWORD=... python tests_live_accounts.py --apply    # do it
"""
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

B = os.getenv("FF_BASE", "https://familyfinancials.onrender.com")
PASSWORD = os.environ["FF_ADMIN_PASSWORD"]
TARGET_VERSION = os.getenv("FF_VERSION", "1.4.0")
APPLY = "--apply" in sys.argv

OPENING_SAVINGS = 260_000_000
LOAN = 300_000_000
LOAN_LENDER = "Prosindo"
LOAN_DATE = "2026-03-01"
LOAN_FUNDED = 230_018_462
LOAN_FUNDED_DATE = "2026-08-21"
STATIC_ASSET = "Prosindo Cash"
STATIC_LIABILITY = "Borrowing - Prosindo"

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
    else:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""), flush=True)


def call(method, path, body=None, token=None, raw=False, tries=4, timeout=180):
    last = None
    for _ in range(tries):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(B + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = r.read()
                if raw:
                    return r.status, payload
                return r.status, json.loads(payload.decode(errors="replace"))
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="replace")
            try:
                return e.code, json.loads(text)
            except Exception:
                return e.code, text[:300]
        except Exception as exc:
            last = exc
            time.sleep(4)
    return None, f"network error: {last!r}"


def close(a, b, tol=2.0):
    return abs(float(a) - float(b)) <= tol


def money(x):
    return f"{float(x):,.0f}"


print(f"waiting for version {TARGET_VERSION} to go live...")
for attempt in range(20):
    s, hz = call("GET", "/healthz")
    if isinstance(hz, dict) and hz.get("version") == TARGET_VERSION:
        print(f"  build {hz['version']} is live (checked {attempt + 1}x)\n")
        break
    print(f"  attempt {attempt + 1}: {hz.get('version') if isinstance(hz, dict) else hz}")
    time.sleep(20)
else:
    print("!! the new build never appeared")
    raise SystemExit(1)

check("database healthy after the upgrade",
      hz.get("database_ok") is True and hz.get("storage_persistent") is True, str(hz.get("backend")))

s, tok = call("POST", "/api/auth/login", {"username": "admin", "password": PASSWORD})
check("admin login", s == 200, f"HTTP {s}")
if s != 200:
    raise SystemExit(1)
TOK = tok["access_token"]

s, bs = call("GET", "/api/statements/balance-sheet", token=TOK)
print("snapshot before the migration:")
print(f"     accounts  : {[(a['name'], a['kind'], money(a['balance'])) for a in bs['accounts']]}")
print(f"     loans     : {money(bs['loans_total'])}   other items: {money(bs['asset_items_total'])}"
      f" / {money(bs['other_liabilities_total'])}")
print(f"     net worth : {money(bs['net_worth'])}")
income = bs["from_activity"]["income"]
expenses = bs["from_activity"]["expenses"]
surplus = income - expenses
expected_net_worth = OPENING_SAVINGS + surplus
print(f"     lifetime surplus {money(surplus)} -> expected net worth after migration "
      f"{money(expected_net_worth)}\n")

params = (f"?opening_savings={OPENING_SAVINGS}&opening_savings_date=2022-01-01"
          f"&loan_amount={LOAN}&loan_lender={LOAN_LENDER}&loan_date={LOAN_DATE}"
          f"&loan_funded_savings_amount={LOAN_FUNDED}&loan_funded_savings_date={LOAN_FUNDED_DATE}"
          f"&remove_static_assets={urllib.parse.quote(STATIC_ASSET)}"
          f"&remove_static_liabilities={urllib.parse.quote(STATIC_LIABILITY)}")

print("== dry run ==")
s, dry = call("POST", "/api/admin/migrate-accounts-model" + params, {}, token=TOK)
check("dry run succeeds", s == 200 and isinstance(dry, dict) and dry.get("ok"), f"HTTP {s}")
if isinstance(dry, dict):
    print("     opening balance :", dry.get("opening_balances_set"))
    print("     withdrawals reclassified:", dry.get("withdrawals_reclassified"))
    print("     loan-funded transfer    :", dry.get("savings_reclassified"))
    print("     borrowing to record     :", dry.get("loan_rows_created"))
    print("     static items to remove  :", dry.get("static_items_removed"))
    for n in dry.get("notes", []):
        print("     note:", n)

if not APPLY:
    print("\nDry run only — re-run with --apply to write the changes.")
    raise SystemExit(0 if failed == 0 else 1)

print("\n== applying ==")
s, res = call("POST", "/api/admin/migrate-accounts-model" + params + "&apply=true", {}, token=TOK)
check("migration applies", s == 200 and isinstance(res, dict) and res.get("ok"), f"HTTP {s}")
print("     before:", {k: (money(v) if isinstance(v, (int, float)) else v)
                       for k, v in (res.get("before") or {}).items()})
print("     after :", {k: (money(v) if isinstance(v, (int, float)) else len(v))
                       for k, v in (res.get("after") or {}).items()})
check("the loan-funded transfer was marked", res.get("savings_reclassified") == 1,
      str(res.get("savings_reclassified")))
check("the borrowing was recorded", res.get("loan_rows_created") == 1, str(res.get("loan_rows_created")))
check("the static asset and liability were replaced",
      len(res.get("static_items_removed") or []) == 2, str(res.get("static_items_removed")))

s, again = call("POST", "/api/admin/migrate-accounts-model" + params + "&apply=true", {}, token=TOK)
check("running it twice is a no-op", s == 200 and again.get("already_applied"),
      f"already_applied={again.get('already_applied')}")

print("\n== the migrated balance sheet ==")
s, bs2 = call("GET", "/api/statements/balance-sheet", token=TOK)
for a in bs2["accounts"]:
    print(f"     {a['name']:<10} opening {money(a['opening_balance'])} + in {money(a['movements_in'])}"
          f" - out {money(a['movements_out'])} = {money(a['balance'])}")
print(f"     loans {money(bs2['loans_total'])} | other liabilities {money(bs2['other_liabilities_total'])}"
      f" | net worth {money(bs2['net_worth'])}")
check("net worth = opening balances + lifetime surplus",
      close(bs2["reconciliation"]["difference"], 0, 0.5),
      f"expected {money(bs2['reconciliation']['expected_net_worth'])}, actual {money(bs2['net_worth'])}")
check("net worth is now the family's real position, not just the activity",
      close(bs2["net_worth"], expected_net_worth, 5), money(bs2["net_worth"]))
check("savings = opening + surplus + the loan-funded transfer",
      close(bs2["savings_total"], OPENING_SAVINGS + surplus + LOAN_FUNDED, 5),
      money(bs2["savings_total"]))
check("cash holds the rest of the loan",
      close(bs2["cash_total"], LOAN - LOAN_FUNDED, 5), money(bs2["cash_total"]))
check("the loan is tracked as a ledger balance", close(bs2["loans_total"], LOAN, 5),
      money(bs2["loans_total"]))
check("the static Prosindo pair is gone",
      bs2["asset_items_total"] == 0 and bs2["other_liabilities_total"] == 0,
      f"assets {money(bs2['asset_items_total'])}, other liabilities {money(bs2['other_liabilities_total'])}")
check("every account traces: opening + in - out = balance",
      all(close(a["opening_balance"] + a["movements_in"] - a["movements_out"], a["balance"], 1)
          for a in bs2["accounts"]))

print("\n== savings and reconciliation ==")
s, summ = call("GET", "/api/savings/summary?months=60", token=TOK)
aug = [m for m in summ["months"] if m["month"] == "2026-08"][0]
check("2026-08 keeps the real transfer and no longer cancels it",
      close(aug["savings_in_loan"], LOAN_FUNDED, 5) and close(aug["sweep_in"], aug["surplus"], 5),
      f"loan-funded {money(aug['savings_in_loan'])}, sweep in {money(aug['sweep_in'])}")
check("closed months move their surplus into savings",
      all(close(m["savings_net"], m["surplus"] + m["savings_in_loan"], 5)
          for m in summ["months"] if m["closed"]))
check("the savings balance matches the balance sheet",
      close(summ["totals"]["closing_balance"], bs2["savings_total"], 5),
      money(summ["totals"]["closing_balance"]))

s, rec = call("GET", "/api/reconciliation", token=TOK)
print(f"     months needing attention: {rec['totals']['months_with_issues']}")
check("no month is left without an explanation",
      rec["totals"]["months_with_issues"] == 0, str(rec["totals"]["months_with_issues"]))
y2022 = [r for r in rec["rows"] if r["month"].startswith("2022")]
check("2022 shows as deficits funded from savings",
      len(y2022) == 12 and all(r["withdrawal"] > 0 for r in y2022),
      f"{len(y2022)} months funded by withdrawals")
check("cash never goes negative", all(r["cash_balance"] >= -0.5 for r in rec["rows"]),
      f"lowest {money(min(r['cash_balance'] for r in rec['rows']))}")

print("\n== income statement and exports ==")
s, rep = call("GET", "/api/statements/income", token=TOK)
check("borrowing is not income", close(rep["income_total"], income, 5), money(rep["income_total"]))
check("the loan-funded part is called out below the line",
      close(rep["financing"]["savings_in_funded_by_loan"], LOAN_FUNDED, 5),
      money(rep["financing"]["savings_in_funded_by_loan"]))
check("the surplus is untouched by transfers and loans",
      close(rep["surplus"], surplus, 5), money(rep["surplus"]))
check("borrowing shows in the financing block",
      close(rep["financing"]["loan_borrowed"], LOAN, 5), money(rep["financing"]["loan_borrowed"]))

s, csvb = call("GET", "/api/export/csv", token=TOK, raw=True)
head = csvb.decode("utf-8-sig").splitlines()[0] if s == 200 else ""
check("CSV export carries the new columns",
      s == 200 and all(c in head for c in ("Member", "AutoOffset", "Direction", "Lender", "FundedBy")), head)
s, xlsx = call("GET", "/api/export/excel", token=TOK, raw=True)
from openpyxl import load_workbook
wb = load_workbook(io.BytesIO(xlsx))
ws = wb["Transactions"]
rows = list(ws.iter_rows(min_row=2, values_only=True))
header = [c.value for c in ws[1]]
loan_rows = [r for r in rows if r[header.index("Type")] == "Loan"]
check("the workbook holds every row including the loan",
      len(loan_rows) == 1 and ws.max_row - 1 == 932, f"{ws.max_row - 1} rows, {len(loan_rows)} loan(s)")
withdrawals = sum(1 for r in rows if r[header.index("Type")] == "Withdrawal")
check("the reclassified withdrawals are exported", withdrawals >= 20, str(withdrawals))

s, st = call("GET", "/api/admin/backup-status", token=TOK)
check("the reminder bookkeeping still works", s == 200 and st["interval_days"] == 7, f"due={st['due']}")
check("the version is reported", st.get("app_version") == TARGET_VERSION, str(st.get("app_version")))

print(f"\n===== LIVE ACCOUNTS CHECK: {passed} passed, {failed} failed =====")
raise SystemExit(1 if failed else 0)
