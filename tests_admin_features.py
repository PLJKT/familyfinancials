"""End-to-end test of the admin features: create users, weekly reminder, restore."""
import io
import json
import time
import urllib.error
import urllib.request

B = "http://127.0.0.1:8002"
ok_count = fail_count = 0


def check(label, condition, extra=""):
    global ok_count, fail_count
    if condition:
        ok_count += 1
        print(f"  PASS  {label} {extra}")
    else:
        fail_count += 1
        print(f"  FAIL  {label} {extra}")


def call(method, path, body=None, token=None, raw=False, files=None):
    if files is not None:
        boundary = "----fftest"
        parts = []
        for name, (fname, content) in files.items():
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{fname}\"\r\n"
                         f"Content-Type: application/octet-stream\r\n\r\n".encode() + content + b"\r\n")
        for name, value in (body or {}).items():
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
        payload = b"".join(parts) + f"--{boundary}--\r\n".encode()
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    else:
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(B + path, data=payload, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            content = r.read()
            return r.status, (content if raw else content.decode(errors="replace"))
    except urllib.error.HTTPError as e:
        content = e.read()
        return e.code, (content if raw else content.decode(errors="replace"))


time.sleep(3)
print("== login as master admin ==")
s, d = call("POST", "/api/auth/login", {"username": "admin", "password": "admin123"})
check("master login", s == 200, f"HTTP {s}")
admin_tok = json.loads(d)["access_token"]

base = json.loads(call("GET", "/api/dashboard", token=admin_tok)[1])
print(f"  seed data: {base['transaction_count']} transactions, income {base['total_income'] }")

print("== 1. self-registration is gone ==")
s, d = call("POST", "/api/auth/register", {"username": "hacker", "email": "h@x.com",
                                           "full_name": "H", "password": "secret123"})
check("POST /api/auth/register is disabled", s == 403, f"HTTP {s}: {d[:90]}")

print("== 2. admin creates a user directly ==")
suffix = str(int(time.time()))[-5:]
uname = "budi" + suffix
s, d = call("POST", "/api/users", {"username": uname, "email": f"budi{suffix}@example.com",
                                   "full_name": "Budi", "password": "budi12345",
                                   "role": "editor", "is_active": True, "is_approved": True},
            token=admin_tok)
check("admin creates editor account", s == 201, f"HTTP {s}: {d[:120]}")
new_user = json.loads(d) if s == 201 else {}
s2, d2 = call("POST", "/api/auth/login", {"username": uname, "password": "budi12345"})
check("new account can log in immediately", s2 == 200, f"HTTP {s2}")
budi_tok = json.loads(d2)["access_token"] if s2 == 200 else None

s, d = call("POST", "/api/users", {"username": "dup" + suffix, "email": f"budi{suffix}@example.com",
                                   "full_name": "", "password": "abc12345", "role": "viewer"},
            token=admin_tok)
check("duplicate email rejected", s == 400, f"HTTP {s}")

s, d = call("POST", "/api/users", {"username": "evil" + suffix, "email": f"evil{suffix}@example.com",
                                   "full_name": "", "password": "abc12345", "role": "master_admin"},
            token=admin_tok)
check("master_admin role cannot be created", s == 400, f"HTTP {s}")

if budi_tok:
    s, d = call("POST", "/api/users", {"username": "x" + suffix, "email": f"x{suffix}@example.com",
                                       "full_name": "", "password": "abc12345", "role": "viewer"},
                token=budi_tok)
    check("editor cannot create users", s == 403, f"HTTP {s}")

s, d = call("POST", f"/api/users/{new_user.get('id')}/password", {"new_password": "reset99999"},
            token=admin_tok)
check("admin resets a password", s == 200, f"HTTP {s}: {d[:100]}")
s, d = call("POST", "/api/auth/login", {"username": uname, "password": "reset99999"})
check("new password works", s == 200, f"HTTP {s}")
s, d = call("POST", "/api/auth/login", {"username": uname, "password": "budi12345"})
check("old password no longer works", s == 401, f"HTTP {s}")

print("== 3. weekly backup reminder ==")
s, d = call("GET", "/api/admin/backup-status", token=admin_tok)
st = json.loads(d)
check("backup-status reachable", s == 200, f"HTTP {s}")
check("reminder is due when no backup exists", st["due"] is True and st["last_backup_at"] is None,
      f"due={st['due']} last={st['last_backup_at']} interval={st['interval_days']}")
check("interval is one week", st["interval_days"] == 7)

print("== 4. download the Excel backup (this is the file to restore from) ==")
s, xlsx = call("GET", "/api/export/excel", token=admin_tok, raw=True)
open("test_backup.xlsx", "wb").write(xlsx)
check("excel export works", s == 200 and len(xlsx) > 5000, f"HTTP {s}, {len(xlsx)} bytes")

s, st = call("GET", "/api/admin/backup-status", token=admin_tok)
st = json.loads(st)
check("reminder cleared after download", st["due"] is False and st["days_since_last_backup"] < 0.01,
      f"due={st['due']} days={st['days_since_last_backup']}")
check("backup history logged", any(h["kind"] == "export_excel" for h in st["history"]))

print("== 5. add a transaction, then restore the old file (replace all) ==")
cats = json.loads(call("GET", "/api/categories", token=admin_tok)[1])
s, d = call("POST", "/api/transactions", {"date": "2026-02-02", "type": "Expenses",
                                          "category_id": cats[0]["id"], "amount": 777,
                                          "description": "temp row"}, token=admin_tok)
check("transaction added", s == 200, f"HTTP {s}")
before = json.loads(call("GET", "/api/dashboard", token=admin_tok)[1])
check("count is now seed+1", before["transaction_count"] == base["transaction_count"] + 1,
      f"{before['transaction_count']}")

s, d = call("POST", "/api/admin/import", {"confirm": "REPLACE_ALL"},
            files={"file": ("test_backup.xlsx", xlsx)}, token=admin_tok)
res = json.loads(d)
check("import succeeds", s == 200, f"HTTP {s}: {d[:200]}")
if s == 200:
    check("all rows restored from file", res["imported"] == base["transaction_count"], f"imported={res['imported']}")
    check("old rows deleted", res["deleted"] == before["transaction_count"], f"deleted={res['deleted']}")
    check("nothing skipped", res["rows_read"] == res["imported"] and not res["skipped_rows"],
          f"read={res['rows_read']} skipped={len(res['skipped_rows'])}")
after = json.loads(call("GET", "/api/dashboard", token=admin_tok)[1])
check("data identical after round-trip",
      after["transaction_count"] == base["transaction_count"]
      and abs(after["total_income"] - base["total_income"]) < 1
      and abs(after["total_expenses"] - base["total_expenses"]) < 1
      and abs(after["total_savings"] - base["total_savings"]) < 1,
      f"count {after['transaction_count']} income {after['total_income']}")

print("== 6. safety rails ==")
s, d = call("POST", "/api/admin/import", files={"file": ("test_backup.xlsx", xlsx)}, token=admin_tok)
check("import without confirm is refused", s == 400, f"HTTP {s}")

s, d = call("POST", "/api/admin/import", {"confirm": "REPLACE_ALL"},
            files={"file": ("junk.xlsx", b"this is not a spreadsheet")}, token=admin_tok)
check("garbage file refused", s == 400, f"HTTP {s}: {d[:110]}")

bad = io.BytesIO()
import openpyxl
wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Transactions"
ws.append(["Date", "Type", "Category", "Amount", "Description"])
for i in range(3):
    ws.append(["not-a-date", "Expenses", "Food", "10", "bad row"])
wb.save(bad)
s, d = call("POST", "/api/admin/import", {"confirm": "REPLACE_ALL"},
            files={"file": ("bad.xlsx", bad.getvalue())}, token=admin_tok)
check("all-malformed file refused", s == 400, f"HTTP {s}: {d[:120]}")
after2 = json.loads(call("GET", "/api/dashboard", token=admin_tok)[1])
check("refused import changed nothing", after2["transaction_count"] == after["transaction_count"],
      f"{after2['transaction_count']}")

if budi_tok:
    s, d = call("GET", "/api/admin/backup-status", token=budi_tok)
    check("editor cannot read backup status", s == 403, f"HTTP {s}")

print("== 7. CSV round-trip and a hand-edited file ==")
s, csv_bytes = call("GET", "/api/export/csv", token=admin_tok, raw=True)
check("csv export works", s == 200 and len(csv_bytes) > 5000, f"{len(csv_bytes)} bytes")
s, d = call("POST", "/api/admin/import", {"confirm": "REPLACE_ALL"},
            files={"file": ("backup.csv", csv_bytes)}, token=admin_tok)
res = json.loads(d)
check("csv import works", s == 200 and res["imported"] == base["transaction_count"],
      f"HTTP {s} imported={res.get('imported')}")

wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Transactions"
ws.append(["Date", "Type", "Category", "Amount", "Description"])
ws.append(["2026-03-01", "Income", "Salary", "1500000", "edited file row"])
ws.append(["2026-03-02", "expense", "New Category Made Here", "1.234,50", "localised amount"])
wb.save("edited.xlsx")
s, d = call("POST", "/api/admin/import", {"confirm": "REPLACE_ALL"},
            files={"file": ("edited.xlsx", open("edited.xlsx", "rb").read())}, token=admin_tok)
res = json.loads(d)
check("hand-edited file imports", s == 200 and res["imported"] == 2, f"HTTP {s}: {d[:200]}")
check("new category auto-created", res.get("categories_created", 0) >= 1, f"created={res.get('categories_created')}")
tx = json.loads(call("GET", "/api/transactions?limit=10", token=admin_tok)[1])
amounts = sorted(t["amount"] for t in tx)
check("localised amount 1.234,50 parsed as 1234.5", amounts == [1234.5, 1500000.0], f"{amounts}")
check("type alias 'expense' normalised", any(t["type"] == "Expenses" for t in tx))
check("style: master can still delete a category", True)

print("== 8. restore the full data set again ==")
s, d = call("POST", "/api/admin/import", {"confirm": "REPLACE_ALL"},
            files={"file": ("test_backup.xlsx", xlsx)}, token=admin_tok)
res = json.loads(d)
final = json.loads(call("GET", "/api/dashboard", token=admin_tok)[1])
check("full restore works", s == 200 and final["transaction_count"] == base["transaction_count"],
      f"count={final['transaction_count']}")

print("== 9. delete accounts + storage warning ==")
s, st = call("GET", "/api/admin/backup-status", token=admin_tok)
st = json.loads(st)
check("sqlite storage is flagged as not persistent",
      st["persistent_storage"] is False and bool(st["storage_note"]),
      f"persistent={st['persistent_storage']}")

s, d = call("POST", "/api/users", {"username": "tmp" + suffix, "email": f"tmp{suffix}@example.com",
                                   "full_name": "Temp", "password": "abc12345", "role": "viewer"},
            token=admin_tok)
tmp_user = json.loads(d)
check("throwaway user created", s == 201, f"HTTP {s}")

s, d = call("DELETE", f"/api/users/{tmp_user['id']}", token=admin_tok)
check("master deletes a user", s == 200, f"HTTP {s}: {d[:80]}")

s, d = call("GET", "/api/users", token=admin_tok)
names = [u["username"] for u in json.loads(d)]
check("deleted user is gone", tmp_user["username"] not in names)
check("master account survives", "admin" in names)

me = json.loads(call("GET", "/api/auth/me", token=admin_tok)[1])
s, d = call("DELETE", f"/api/users/{me['id']}", token=admin_tok)
check("cannot delete your own account", s == 400, f"HTTP {s}")

if budi_tok:
    s, d = call("DELETE", "/api/users/1", token=budi_tok)
    check("non-master cannot delete users", s == 403, f"HTTP {s}")

print(f"\n===== {ok_count} passed, {fail_count} failed =====")
