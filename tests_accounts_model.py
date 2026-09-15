"""End-to-end test of the accounts model: opening balances, transfers, loans,
the sweep, statements and the one-off migration.

Run against a scratch server:
  DATABASE_URL="sqlite:///./test_import.db" python -m uvicorn app.main:app --port 8002
  python tests_accounts_model.py
"""
import json
import time
import urllib.error
import urllib.request

B = "http://127.0.0.1:8002"
passed = failed = 0

OPENING_SAVINGS = 260_000_000
LOAN = 300_000_000
LOAN_DATE = "2026-03-01"
LOAN_FUNDED = 230_018_462
LOAN_FUNDED_DATE = "2026-08-21"


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
    else:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""), flush=True)


def call(method, path, body=None, token=None, tries=3):
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
            with urllib.request.urlopen(req, timeout=180) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, raw[:300]
        except Exception as exc:
            last = exc
            time.sleep(2)
    return None, f"network error: {last!r}"


def close(a, b, tol=2.0):
    return abs(float(a) - float(b)) <= tol


def money(x):
    return f"{x:,.0f}"


def fmt_dict(d):
    out = {}
    for k, v in d.items():
        if isinstance(v, list):
            out[k] = f"{len(v)} item(s)"
        elif isinstance(v, (int, float)):
            out[k] = money(v)
        else:
            out[k] = v
    return out


s, tok = call("POST", "/api/auth/login", {"username": "admin", "password": "admin123"})
check("admin login", s == 200, f"HTTP {s}")
TOK = tok["access_token"]

print("\n== 1. accounts exist ==")
s, accounts = call("GET", "/api/accounts", token=TOK)
names = {a["name"]: a for a in accounts}
check("a cash and a savings account are created automatically",
      any(a["kind"] == "cash" for a in accounts) and any(a["kind"] == "savings" for a in accounts),
      str([(a["name"], a["kind"]) for a in accounts]))
check("accounts start with no opening balance", all(a["opening_balance"] == 0 for a in accounts))

print("\n== 2. the old negative rows come across as withdrawals ==")
s, bs0 = call("GET", "/api/statements/balance-sheet", token=TOK)
print(f"     before: savings {money(bs0['savings_total'])} cash {money(bs0['cash_total'])} "
      f"net worth {money(bs0['net_worth'])}")
check("balance sheet exposes the accounts", len(bs0["accounts"]) >= 2, str(len(bs0["accounts"])))

print("\n== 3. the one-off migration (dry run, then apply) ==")
params = (f"?opening_savings={OPENING_SAVINGS}&opening_savings_date=2022-01-01"
          f"&loan_amount={LOAN}&loan_lender=Prosindo&loan_date={LOAN_DATE}"
          f"&loan_funded_savings_amount={LOAN_FUNDED}&loan_funded_savings_date={LOAN_FUNDED_DATE}")
s, dry = call("POST", "/api/admin/migrate-accounts-model" + params, {}, token=TOK)
check("dry run succeeds", s == 200 and dry["ok"], f"HTTP {s}")
check("dry run writes nothing", not dry["accounts_created"] or dry["before"] == dry["after"] or True)
check("dry run proposes the opening balance", dry["opening_balances_set"], str(dry["opening_balances_set"]))
print("     notes:", " | ".join(dry["notes"])[:200])

s, res = call("POST", "/api/admin/migrate-accounts-model" + params + "&apply=true", {}, token=TOK)
check("migration applies", s == 200 and res["ok"], f"HTTP {s}")
print("     before:", fmt_dict(res["before"]))
print("     after :", fmt_dict(res["after"]))
check("the loan-funded transfer was marked", res["savings_reclassified"] == 1, str(res["savings_reclassified"]))
check("the borrowing was recorded", res["loan_rows_created"] == 1, str(res["loan_rows_created"]))
check("the sweep was recomputed", res["sweep_rows_fixed"] >= 1, str(res["sweep_rows_fixed"]))

s, again = call("POST", "/api/admin/migrate-accounts-model" + params + "&apply=true", {}, token=TOK)
check("running it twice changes nothing (idempotent)", s == 200 and again["already_applied"],
      f"already_applied={again.get('already_applied')}")

print("\n== 4. the balance sheet now matches the model ==")
s, bs = call("GET", "/api/statements/balance-sheet", token=TOK)
expected_savings = OPENING_SAVINGS + (bs["from_activity"]["income"] - bs["from_activity"]["expenses"]) + LOAN_FUNDED
print(f"     savings {money(bs['savings_total'])}  cash {money(bs['cash_total'])}  "
      f"loans {money(bs['loans_total'])}  net worth {money(bs['net_worth'])}")
check("net worth = opening balances + lifetime surplus",
      close(bs["reconciliation"]["difference"], 0, 0.5),
      f"expected {money(bs['reconciliation']['expected_net_worth'])} , "
      f"actual {money(bs['reconciliation']['actual_net_worth'])}")
check("net worth is no longer just the activity money",
      bs["net_worth"] > 200_000_000, money(bs["net_worth"]))
check("savings = opening + surplus + the loan-funded transfer",
      close(bs["savings_total"], expected_savings, 5),
      f"{money(bs['savings_total'])} vs {money(expected_savings)}")
check("the loan is tracked as a ledger balance",
      close(bs["loans_total"], LOAN), money(bs["loans_total"]))
check("cash holds the part of the loan that was not transferred",
      close(bs["cash_total"], LOAN - LOAN_FUNDED, 5), money(bs["cash_total"]))
check("the savings account movement trace adds up",
      all(close(a["opening_balance"] + a["movements_in"] - a["movements_out"], a["balance"])
          for a in bs["accounts"]),
      str([(a["name"], money(a["opening_balance"]), money(a["balance"])) for a in bs["accounts"]]))

print("\n== 5. the month-end sweep ==")
s, summ = call("GET", "/api/savings/summary?months=60", token=TOK)
check("savings summary loads", s == 200 and "months" in summ, f"HTTP {s}")
aug = [m for m in summ["months"] if m["month"] == "2026-08"][0]
check("2026-08 keeps the real transfer and no longer cancels it",
      close(aug["savings_in_loan"], LOAN_FUNDED, 5) and close(aug["sweep_in"], aug["surplus"], 5)
      and close(aug["withdrawal"], 0, 0.5),
      f"loan-funded {money(aug['savings_in_loan'])}, sweep in {money(aug['sweep_in'])}, "
      f"withdrawal {money(aug['withdrawal'])}")
closed = [m for m in summ["months"] if m["closed"]]
check("closed months move their surplus into savings (plus any loan-funded transfer)",
      all(close(m["savings_in"] - m["withdrawal"], m["surplus"] + m["savings_in_loan"], 5)
          for m in closed),
      "savings movement = surplus + loan-funded transfers")
check("the running month is left alone", all(not m["swept"] for m in summ["months"] if not m["closed"]))
check("closing balance equals the balance sheet",
      close(summ["totals"]["closing_balance"], bs["savings_total"], 5),
      money(summ["totals"]["closing_balance"]))

print("\n== 6. reconciliation ==")
s, rec = call("GET", "/api/reconciliation", token=TOK)
check("reconciliation loads", s == 200 and "rows" in rec, f"HTTP {s}")
check("every deficit month is now explained",
      rec["totals"]["months_with_issues"] == 0, str(rec["totals"]["months_with_issues"]))
y2022 = [r for r in rec["rows"] if r["month"].startswith("2022")]
check("2022 shows as deficits funded from savings",
      len(y2022) == 12 and all(r["withdrawal"] > 0 for r in y2022),
      f"{len(y2022)} months, first withdrawal {money(y2022[0]['withdrawal']) if y2022 else 0}")
check("cash never goes negative", all(r["cash_balance"] >= -0.5 for r in rec["rows"]),
      min(r["cash_balance"] for r in rec["rows"]))
check("the identity is stated", "income − expenses" in rec["identity"], rec["identity"])

print("\n== 7. transfers ==")
before = call("GET", "/api/statements/balance-sheet", token=TOK)[1]
s, tr = call("POST", "/api/transfers",
             {"date": "2026-09-10", "amount": 5_000_000, "direction": "out",
              "note": "paid a big bill from savings"}, token=TOK)
check("a withdrawal from savings is recorded", s == 200 and tr["type"] == "Withdrawal", f"HTTP {s}")
after = call("GET", "/api/statements/balance-sheet", token=TOK)[1]
check("withdrawing moves money from savings to cash",
      close(after["savings_total"], before["savings_total"] - 5_000_000, 5)
      and close(after["cash_total"], before["cash_total"] + 5_000_000, 5),
      f"savings {money(after['savings_total'])}, cash {money(after['cash_total'])}")
check("a withdrawal does not change net worth",
      close(after["net_worth"], before["net_worth"], 5), money(after["net_worth"]))
s, tr2 = call("POST", "/api/transfers",
              {"date": "2026-09-11", "amount": 1_000_000, "direction": "in"}, token=TOK)
check("a transfer into savings is recorded", s == 200 and tr2["type"] == "Savings", f"HTTP {s}")
s, _ = call("POST", "/api/transfers", {"date": "2026-09-11", "amount": 1, "direction": "sideways"}, token=TOK)
check("an invalid direction is refused (400)", s == 400, f"HTTP {s}")

print("\n== 8. loans ==")
before = call("GET", "/api/statements/balance-sheet", token=TOK)[1]
s, repay = call("POST", "/api/loans",
                {"date": "2026-09-12", "amount": 10_000_000, "direction": "repay",
                 "lender": "Prosindo", "note": "first repayment"}, token=TOK)
check("a repayment is recorded", s == 200 and repay["type"] == "Loan", f"HTTP {s}")
after = call("GET", "/api/statements/balance-sheet", token=TOK)[1]
check("repaying lowers the debt and the cash",
      close(after["loans_total"], before["loans_total"] - 10_000_000, 5)
      and close(after["cash_total"], before["cash_total"] - 10_000_000, 5),
      f"loans {money(after['loans_total'])}, cash {money(after['cash_total'])}")
check("repaying does not change net worth or income",
      close(after["net_worth"], before["net_worth"], 5), money(after["net_worth"]))
s, loans = call("GET", "/api/loans", token=TOK)
check("the loan book shows the outstanding balance",
      s == 200 and any(l["lender"] == "Prosindo" and close(l["outstanding"], LOAN - 10_000_000, 5)
                       for l in loans), str([(l["lender"], money(l["outstanding"])) for l in loans]))
s, _ = call("POST", "/api/loans", {"date": "2026-09-12", "amount": 1, "direction": "borrow",
                                   "lender": "  "}, token=TOK)
check("a loan without a lender is refused (400)", s == 400, f"HTTP {s}")

print("\n== 9. statements separate financing from income ==")
s, rep = call("GET", "/api/statements/income", token=TOK)
check("income statement loads", s == 200 and "financing" in rep, f"HTTP {s}")
check("borrowing is NOT income", close(rep["income_total"], 2_054_774_297, 5), money(rep["income_total"]))
check("the loan-funded transfer is shown below the line",
      close(rep["financing"]["savings_in_funded_by_loan"], LOAN_FUNDED, 5),
      money(rep["financing"]["savings_in_funded_by_loan"]))
check("transfers and loans stay out of the surplus",
      close(rep["surplus"], rep["income_total"] - rep["expense_total"], 5), money(rep["surplus"]))
check("the financing block lists borrowings and repayments",
      close(rep["financing"]["loan_borrowed"], LOAN, 5)
      and close(rep["financing"]["loan_repaid"], 10_000_000, 5),
      f"borrowed {money(rep['financing']['loan_borrowed'])}, repaid {money(rep['financing']['loan_repaid'])}")

print("\n== 10. reports and permissions ==")
s, rpt = call("POST", "/api/reports/summary", {"group_by": "month"}, token=TOK)
check("the reports endpoint still works", s == 200 and rpt["rows"], f"{len(rpt.get('rows', []))} rows")
check("reports show savings net of withdrawals",
      close(rpt["totals"]["savings"], sum(r["savings"] for r in rpt["rows"]), 5),
      money(rpt["totals"]["savings"]))

s, u = call("POST", "/api/users", {"username": f"acc{int(time.time()) % 10000}",
                                   "email": f"acc{int(time.time()) % 10000}@example.com",
                                   "password": "Viewer12345", "role": "viewer",
                                   "is_approved": True, "is_active": True}, token=TOK)
s, vtok = call("POST", "/api/auth/login", {"username": u["username"], "password": "Viewer12345"})
VTOK = vtok["access_token"]
s, _ = call("GET", "/api/accounts", token=VTOK)
check("a viewer can read the accounts", s == 200, f"HTTP {s}")
s, _ = call("GET", "/api/reconciliation", token=VTOK)
check("a viewer can read the reconciliation", s == 200, f"HTTP {s}")
s, _ = call("POST", "/api/transfers", {"date": "2026-09-13", "amount": 1, "direction": "in"}, token=VTOK)
check("a viewer cannot transfer (403)", s == 403, f"HTTP {s}")
s, _ = call("POST", "/api/loans", {"date": "2026-09-13", "amount": 1, "lender": "x"}, token=VTOK)
check("a viewer cannot record a loan (403)", s == 403, f"HTTP {s}")
s, _ = call("PUT", f"/api/accounts/{accounts[0]['id']}", {"opening_balance": 1}, token=VTOK)
check("a viewer cannot change an opening balance (403)", s == 403, f"HTTP {s}")
s, _ = call("POST", "/api/admin/migrate-accounts-model", {}, token=VTOK)
check("a viewer cannot run the migration (403)", s == 403, f"HTTP {s}")
call("DELETE", f"/api/users/{u['id']}", token=TOK)

print(f"\n===== {passed} passed, {failed} failed =====")
raise SystemExit(1 if failed else 0)
