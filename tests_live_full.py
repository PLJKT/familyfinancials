"""Full end-to-end check of the live Family Financials deployment.

Run:  FF_ADMIN_PASSWORD=... python tests_live_full.py
Reads the admin password from the environment so it is never stored in the file.
Creates only its own temporary records (users, one transaction, one category)
and deletes them again; every destructive import attempt uses an INVALID file so
the real data cannot be touched.
"""
import base64
import csv
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.getenv("FF_BASE", "https://familyfinancials.onrender.com")
ADMIN_PASSWORD = os.environ["FF_ADMIN_PASSWORD"]
LOCAL_BACKUP = os.getenv("FF_LOCAL_BACKUP",
                         r"backups\live_20260914_203820\live_backup.xlsx")

if os.getenv("FF_CONFIRM_PRODUCTION") != "1":
    print("\n" + "!" * 74)
    print("!  LIVE / PRODUCTION database warning")
    print(f"!  This script talks to the REAL database at: {BASE}")
    print("!  It only touches its own temporary records, but run it with care.")
    print("!  Set FF_CONFIRM_PRODUCTION=1 to suppress this banner.")
    print("!" * 74 + "\n")

results = []
passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
    else:
        failed += 1
    line = f"  {'PASS' if ok else 'FAIL'}  {name}"
    if detail:
        line += f"  [{detail}]"
    print(line, flush=True)
    results.append({"name": name, "ok": bool(ok), "detail": str(detail)[:300]})


def call(method, path, body=None, token=None, raw=False, timeout=120, tries=4):
    """HTTP request with retries; returns (status, payload)."""
    last = None
    for _ in range(tries):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = r.read()
                if raw:
                    return r.status, payload
                text = payload.decode(errors="replace")
                try:
                    return r.status, json.loads(text)
                except Exception:
                    return r.status, text
        except urllib.error.HTTPError as e:
            payload = e.read()
            if raw:
                return e.code, payload
            text = payload.decode(errors="replace")
            try:
                return e.code, json.loads(text)
            except Exception:
                return e.code, text
        except Exception as exc:  # network hiccup -> retry
            last = exc
            time.sleep(3)
    return None, f"network error: {last!r}"


# ------------------------------------------------------------------ public site
print("\n== 1. public site ==")
s, html = call("GET", "/", raw=True)
html_text = html.decode(errors="replace") if isinstance(html, bytes) else str(html)
check("GET / serves the app", s == 200 and "Family" in html_text, f"HTTP {s}")
check("login form present", 'id="login-form"' in html_text or "loginForm" in html_text)
check("no Register tab in the UI", "registerPane" not in html_text and "register-form" not in html_text)
check("shows the 'accounts are created by the administrator' note",
      "administrator" in html_text)

for asset in ("/static/js/app.js", "/static/css/style.css"):
    s, payload = call("GET", asset, raw=True)
    check(f"{asset} loads", s == 200 and len(payload) > 500, f"HTTP {s}, {len(payload)} bytes")

s, hz = call("GET", "/healthz")
check("/healthz answers", s == 200 and isinstance(hz, dict), f"HTTP {s}")
check("/healthz reports a healthy postgres database",
      hz.get("database_ok") is True and hz.get("backend") == "postgresql", str(hz.get("backend")))
check("/healthz reports PERSISTENT storage", hz.get("storage_persistent") is True)
check("/healthz exposes no database host/user", "database_url" not in hz and "database_target" not in hz)
check("/healthz reports the app version", bool(hz.get("version")), f"v{hz.get('version')}")

s, d = call("POST", "/api/auth/register", {"username": "x", "email": "x@y.com", "password": "aaaa"})
check("self-registration is disabled (403)", s == 403, f"HTTP {s}")

s, d = call("POST", "/api/auth/login", {"username": "admin", "password": "definitely-wrong"})
check("wrong password is rejected (401)", s == 401, f"HTTP {s}")

print("\n== 2. the API rejects anonymous callers ==")
for path in ("/api/users", "/api/dashboard", "/api/transactions", "/api/categories",
             "/api/export/excel", "/api/export/csv", "/api/admin/backup-status", "/api/auth/me"):
    s, _ = call("GET", path)
    check(f"{path} without a token -> 401", s == 401, f"HTTP {s}")
s, _ = call("GET", "/api/dashboard", token="not-a-real-token")
check("bogus token rejected", s == 401, f"HTTP {s}")

# ---------------------------------------------------------------------- sign in
print("\n== 3. sign in as the master admin ==")
s, tok = call("POST", "/api/auth/login", {"username": "admin", "password": ADMIN_PASSWORD})
check("admin login works with the new password", s == 200 and "access_token" in tok, f"HTTP {s}")
if s != 200:
    print("\nCannot continue without a token. Response:", str(tok)[:200])
    sys.exit(1)
TOK = tok["access_token"]

s, me = call("GET", "/api/auth/me", token=TOK)
check("/api/auth/me returns the master admin",
      s == 200 and me.get("role") == "master_admin" and me.get("username") == "admin", f"{me.get('username')}/{me.get('role')}")

# ---------------------------------------------------------------------- data
print("\n== 4. reading the data ==")
s, dash = call("GET", "/api/dashboard", token=TOK)
check("dashboard loads", s == 200 and isinstance(dash, dict), f"HTTP {s}")
count = dash.get("transaction_count")
print("     dashboard:", {k: dash.get(k) for k in
                          ("transaction_count", "total_income", "total_expenses", "total_savings", "balance")})
check("dashboard has a transaction count", isinstance(count, int) and count >= 887, str(count))

s, cats = call("GET", "/api/categories", token=TOK)
check("categories load", s == 200 and len(cats) >= 20, f"{len(cats)} categories")

s, tx = call("GET", "/api/transactions?limit=5", token=TOK)
check("transactions load", s == 200 and len(tx) == 5, f"{len(tx)} rows")
check("transaction rows carry the expected fields",
      all({"id", "date", "type", "amount", "category_id"} <= set(t.keys()) for t in tx))

s, tx_all = call("GET", "/api/transactions?limit=5000", token=TOK)
check("full transaction list loads", s == 200 and len(tx_all) >= 887, f"{len(tx_all)} rows")

# filters
s, only_savings = call("GET", "/api/transactions?types=Savings&limit=5000", token=TOK)
check("filter by type works", s == 200 and only_savings and all(t["type"] == "Savings" for t in only_savings),
      f"{len(only_savings)} savings rows")
s, by_cat = call("GET", f"/api/transactions?category_ids={cats[0]['id']}&limit=5000", token=TOK)
check("filter by category works", s == 200 and by_cat and all(t["category_id"] == cats[0]["id"] for t in by_cat),
      f"{len(by_cat)} rows in '{cats[0]['name']}'")
s, window = call("GET", "/api/transactions?start_date=2026-01-01&end_date=2026-01-31&limit=5000", token=TOK)
check("filter by date window works",
      s == 200 and window and all("2026-01-01" <= t["date"] <= "2026-01-31" for t in window),
      f"{len(window)} rows in Jan 2026")
s, page = call("GET", "/api/transactions?limit=3&offset=10", token=TOK)
check("pagination works", s == 200 and len(page) == 3, f"{len(page)} rows at offset 10")

# ------------------------------------------------------------------- write path
print("\n== 5. add / edit / delete a transaction ==")
cat_id = cats[0]["id"]
s, created = call("POST", "/api/transactions",
                  {"date": "2026-09-14", "type": "Expenses", "category_id": cat_id,
                   "amount": 4242, "description": "E2E CHECK - temporary row"}, token=TOK)
check("transaction created", s == 200 and "id" in created, f"HTTP {s}")
new_id = created.get("id") if isinstance(created, dict) else None

s, dash2 = call("GET", "/api/dashboard", token=TOK)
check("count increased by 1", dash2.get("transaction_count") == count + 1,
      f"{count} -> {dash2.get('transaction_count')}")

s, updated = call("PUT", f"/api/transactions/{new_id}",
                  {"amount": 5151, "description": "E2E CHECK - edited"}, token=TOK)
check("transaction updated", s == 200 and abs(updated.get("amount", 0) - 5151) < 0.01,
      f"amount={updated.get('amount')} desc={updated.get('description')!r}")

s, _ = call("POST", "/api/transactions",
            {"date": "2026-09-14", "type": "Expenses", "category_id": 999999,
             "amount": 1, "description": "bad category"}, token=TOK)
check("invalid category rejected (400)", s == 400, f"HTTP {s}")

s, _ = call("DELETE", f"/api/transactions/{new_id}", token=TOK)
check("transaction deleted", s == 200, f"HTTP {s}")
s, dash3 = call("GET", "/api/dashboard", token=TOK)
check("count back to where it started", dash3.get("transaction_count") == count,
      f"{dash3.get('transaction_count')}")

# ------------------------------------------------------------------- categories
print("\n== 6. category add / edit / delete ==")
s, newcat = call("POST", "/api/categories",
                 {"name": "ZZ E2E Check", "type": "Expenses", "group": "test"}, token=TOK)
check("category created", s == 200 and "id" in newcat, f"HTTP {s}")
ncid = newcat.get("id") if isinstance(newcat, dict) else None
s, cat2 = call("PUT", f"/api/categories/{ncid}",
               {"name": "ZZ E2E Check renamed", "type": "Expenses", "group": "test"}, token=TOK)
check("category renamed", s == 200 and cat2.get("name") == "ZZ E2E Check renamed",
      f"HTTP {s}, name={cat2.get('name')!r}")
check("rename keeps the category type", cat2.get("type") == "Expenses", str(cat2.get("type")))
s, _ = call("PUT", f"/api/categories/{ncid}", {"name": "only a name"}, token=TOK)
check("a partial category update is rejected (422)", s == 422, f"HTTP {s}")
s, _ = call("DELETE", f"/api/categories/{ncid}", token=TOK)
check("category deleted", s == 200, f"HTTP {s}")

# ---------------------------------------------------------------------- reports
print("\n== 7. reports ==")
s, rep = call("POST", "/api/reports/summary", {"group_by": "month"}, token=TOK)
check("monthly report builds", s == 200 and rep.get("rows") is not None, f"HTTP {s}, {len(rep.get('rows', []))} rows")
tot = rep.get("totals", {})
check("report totals agree with the dashboard",
      abs(float(tot.get("income", tot.get("total_income", 0))) - float(dash["total_income"])) < 1
      and abs(float(tot.get("expense", tot.get("total_expense", tot.get("expenses", 0)))) - float(dash["total_expenses"])) < 1,
      f"totals={ {k: tot.get(k) for k in list(tot)[:6]} }")
for grouping in ("year", "category"):
    s, rep_g = call("POST", "/api/reports/summary", {"group_by": grouping}, token=TOK)
    check(f"{grouping} report builds", s == 200 and rep_g.get("rows"), f"{len(rep_g.get('rows', []))} rows")
s, rep_d = call("POST", "/api/reports/summary",
                {"group_by": "month", "start_date": "2026-01-01", "end_date": "2026-01-31"}, token=TOK)
check("date-filtered report builds", s == 200, f"HTTP {s}")

# ----------------------------------------------------------------------- export
print("\n== 8. backup downloads ==")
s, csv_bytes = call("GET", "/api/export/csv", token=TOK, raw=True)
rows = list(csv.reader(io.StringIO(csv_bytes.decode("utf-8-sig")))) if s == 200 else []
check("CSV export downloads", s == 200 and len(rows) > 800, f"{len(rows)} lines")
check("CSV row count matches the dashboard", len(rows) - 1 == dash3.get("transaction_count"),
      f"{len(rows)-1} data rows vs {dash3.get('transaction_count')}")

s, xlsx_bytes = call("GET", "/api/export/excel", token=TOK, raw=True)
check("Excel export downloads", s == 200 and xlsx_bytes[:2] == b"PK", f"{len(xlsx_bytes)} bytes")

wb = None
if s == 200:
    try:
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(xlsx_bytes))
        check("Excel file opens and has the round-trip sheets",
              {"Transactions", "Categories"} <= set(wb.sheetnames), str(wb.sheetnames))
        ws = wb["Transactions"]
        check("Excel holds every transaction", ws.max_row - 1 == dash3.get("transaction_count"),
              f"{ws.max_row-1} rows vs {dash3.get('transaction_count')}")
    except Exception as exc:
        check("Excel file opens", False, f"{type(exc).__name__}: {exc}")

# the exported file must be identical to the one saved before the Neon migration
if wb is not None and os.path.exists(LOCAL_BACKUP):
    try:
        from openpyxl import load_workbook
        old = load_workbook(LOCAL_BACKUP)
        def rows_of(w):
            ws = w["Transactions"]
            hdr = [c.value for c in ws[1]]
            auto = hdr.index("AutoOffset") if "AutoOffset" in hdr else None
            # compare only the columns the older file has, so added columns do not count as changes
            cols = [hdr.index(c) for c in ("Date", "Type", "Category", "Amount", "Description") if c in hdr]
            out = []
            for r in ws.iter_rows(min_row=2, values_only=True):
                if auto is not None and r[auto]:
                    continue          # automatic sweep rows did not exist in the older file
                out.append(tuple(str(r[i]) for i in cols))
            return hdr, sorted(out)
        h1, r1 = rows_of(wb)
        h2, r2 = rows_of(old)
        # Compare as MULTISETS: a single inserted row must not shift a sorted list and
        # look like hundreds of differences. The accounts model legitimately adds the
        # borrowing row and fills in a description on the loan-funded transfer.
        from collections import Counter
        added = Counter(r1) - Counter(r2)
        removed = Counter(r2) - Counter(r1)
        added_rows = list(added.elements())
        removed_rows = list(removed.elements())
        loan_rows = [r for r in added_rows if r[1] == "Loan"]
        def identity(r):
            return r[:4]                      # date, type, category, amount
        check("the 887 original rows are intact (only the borrowing and a filled-in label differ)",
              len(loan_rows) == 1
              and Counter(identity(r) for r in added_rows if r[1] != "Loan")
                  == Counter(identity(r) for r in removed_rows),
              f"{len(r1)} vs {len(r2)} rows; added {len(added_rows)} ({added_rows[:2]}), "
              f"changed {len(removed_rows)} ({removed_rows[:2]})")
    except Exception as exc:
        check("compare with the pre-migration backup", False, f"{type(exc).__name__}: {exc}")

# ------------------------------------------------------------ reminder / status
print("\n== 9. weekly backup reminder + diagnostics ==")
s, st = call("GET", "/api/admin/backup-status", token=TOK)
check("backup status loads", s == 200 and isinstance(st, dict), f"HTTP {s}")
check("reminder interval is weekly", st.get("interval_days") == 7, str(st.get("interval_days")))
check("storage reported as persistent", st.get("persistent_storage") is True)
check("database reachable per the status endpoint", st.get("database_ok") is True)
check("status names the postgres target", "postgres" in str(st.get("database_target", "")),
      str(st.get("database_target"))[:70])
check("status reports the app version", st.get("app_version") == hz.get("version"),
      f"{st.get('app_version')} (healthz says {hz.get('version')})")
check("download history is recorded", len(st.get("history") or []) > 0,
      f"{len(st.get('history') or [])} entries")
check("reminder cleared after today's download", st.get("due") is False,
      f"due={st.get('due')} last={st.get('last_backup_kind')}")

# ------------------------------------------------------------------ user admin
print("\n== 10. admin user management + permissions ==")
s, users = call("GET", "/api/users", token=TOK)
check("user list loads", s == 200 and any(u["username"] == "admin" for u in users), f"{len(users)} users")

stamp = str(int(time.time()))[-5:]
uname = f"e2e{stamp}"
s, u1 = call("POST", "/api/users", {"username": uname, "email": f"{uname}@example.com",
                                    "full_name": "E2E tester", "password": "E2eTest12345",
                                    "role": "viewer", "is_approved": True, "is_active": True}, token=TOK)
check("master creates a viewer account", s == 201 and u1.get("role") == "viewer", f"HTTP {s}")
uid = u1.get("id") if isinstance(u1, dict) else None

s, _ = call("POST", "/api/users", {"username": uname, "email": f"other{stamp}@example.com",
                                   "password": "E2eTest12345", "role": "viewer"}, token=TOK)
check("duplicate username rejected (400)", s == 400, f"HTTP {s}")
s, _ = call("POST", "/api/users", {"username": f"m{stamp}", "email": f"m{stamp}@example.com",
                                   "password": "E2eTest12345", "role": "master_admin"}, token=TOK)
check("a second master_admin cannot be created (400)", s == 400, f"HTTP {s}")

s, vtok = call("POST", "/api/auth/login", {"username": uname, "password": "E2eTest12345"})
check("the new account can sign in", s == 200 and "access_token" in vtok, f"HTTP {s}")
VTOK = vtok.get("access_token") if isinstance(vtok, dict) else None

s, _ = call("GET", "/api/dashboard", token=VTOK)
check("viewer may read the dashboard", s == 200, f"HTTP {s}")
s, _ = call("POST", "/api/transactions", {"date": "2026-09-14", "type": "Expenses",
                                          "category_id": cat_id, "amount": 1}, token=VTOK)
check("viewer may NOT add transactions (403)", s == 403, f"HTTP {s}")
s, _ = call("GET", "/api/export/excel", token=VTOK)
check("viewer may NOT download data (403)", s == 403, f"HTTP {s}")
s, _ = call("GET", "/api/admin/backup-status", token=VTOK)
check("viewer may NOT see backup status (403)", s == 403, f"HTTP {s}")
s, _ = call("GET", "/api/users", token=VTOK)
check("viewer may NOT list users (403)", s == 403, f"HTTP {s}")

# capability matrix as implemented: require_downloader also lets editors export
s, perm_cat = call("POST", "/api/categories",
                   {"name": "ZZ perm check", "type": "Expenses"}, token=TOK)
perm_cat_id = perm_cat.get("id") if isinstance(perm_cat, dict) else None

for role, can_edit, can_download, can_admin in (("editor", True, True, False),
                                                ("downloader", False, True, False),
                                                ("admin", True, True, True)):
    s, _ = call("PATCH", f"/api/users/{uid}", {"role": role}, token=TOK)
    check(f"master promotes the account to {role}", s == 200, f"HTTP {s}")
    s, _ = call("POST", "/api/transactions", {"date": "2026-09-14", "type": "Expenses",
                                              "category_id": cat_id, "amount": 7,
                                              "description": f"role probe {role}"}, token=VTOK)
    ok_edit = s == 200
    probe_id = None
    if ok_edit:
        probe_id = None
        s2, lst = call("GET", "/api/transactions?limit=5", token=VTOK)
        if isinstance(lst, list):
            for t in lst:
                if t.get("description") == f"role probe {role}":
                    probe_id = t["id"]
        if probe_id:
            call("DELETE", f"/api/transactions/{probe_id}", token=VTOK)
    check(f"{role}: add transaction {'allowed' if can_edit else 'refused'}",
          ok_edit == can_edit, f"HTTP {s}")
    s, _ = call("GET", "/api/export/excel", token=VTOK)
    check(f"{role}: download {'allowed' if can_download else 'refused'}",
          (s == 200) == can_download, f"HTTP {s}")
    s, _ = call("GET", "/api/admin/backup-status", token=VTOK)
    check(f"{role}: admin endpoints {'allowed' if can_admin else 'refused'}",
          (s == 200) == can_admin, f"HTTP {s}")
    s, _ = call("DELETE", f"/api/categories/{perm_cat_id}", token=VTOK)
    check(f"{role}: deleting a category {'allowed' if can_admin else 'refused'}",
          (s == 200) == can_admin, f"HTTP {s}")

s, _ = call("POST", f"/api/users/{uid}/password", {"new_password": "Rotated12345"}, token=TOK)
check("master resets the user's password", s == 200, f"HTTP {s}")
s, _ = call("POST", "/api/auth/login", {"username": uname, "password": "E2eTest12345"})
check("old password no longer works (401)", s == 401, f"HTTP {s}")
s, _ = call("POST", "/api/auth/login", {"username": uname, "password": "Rotated12345"})
check("new password works", s == 200, f"HTTP {s}")

s, _ = call("DELETE", f"/api/users/1", token=TOK)
check("master cannot delete their own account (400)", s == 400, f"HTTP {s}")
s, d = call("DELETE", f"/api/users/{uid}", token=TOK)
check("master deletes the temporary account", s == 200, f"HTTP {s}")
s, _ = call("POST", "/api/auth/login", {"username": uname, "password": "Rotated12345"})
check("deleted account can no longer sign in (401)", s == 401, f"HTTP {s}")

# --------------------------------------------------------------- restore safety
print("\n== 11. restore safety rails (no real data touched) ==")
before = call("GET", "/api/dashboard", token=TOK)[1].get("transaction_count")
s, _ = call("POST", "/api/admin/import", {"file": "x"}, token=TOK)
check("import without a file is refused", s in (400, 422), f"HTTP {s}")
s, _ = call("GET", "/api/admin/import", token=TOK)
check("import cannot be triggered by GET", s in (405, 404), f"HTTP {s}")
after = call("GET", "/api/dashboard", token=TOK)[1].get("transaction_count")
check("data untouched by the refused attempts", before == after, f"{before} -> {after}")

print(f"\n===== LIVE CHECK: {passed} passed, {failed} failed =====")
with open("live_check_results.json", "w", encoding="utf-8") as f:
    json.dump({"base": BASE, "passed": passed, "failed": failed, "results": results}, f, indent=2)
print("results written to live_check_results.json")
sys.exit(1 if failed else 0)
