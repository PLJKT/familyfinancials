# Family Financial Control System

An online, login‑protected family budget control system built with **FastAPI + SQLite/PostgreSQL**.
It ships with the existing **887 transactions** from the revised Excel workbook and provides:

- Secure login — **accounts are created by the administrator** (there is no public sign‑up)
- Role‑based access: `master_admin`, `admin`, `editor`, `viewer`, `downloader`
- Add / edit / delete transactions with category dropdowns
- **Savings page** — record saving amounts (per family member) and see the automatic month‑end sweep
- **Automatic month‑end saving offset** — each closed month's savings are reconciled with its surplus
- **Financial statements** — income statement and balance sheet (assets, liabilities, net worth)
- Automatic financial reports grouped by month, year, or category
- Dashboard with KPIs and charts (income, expenses, savings, transactions)
- CSV / Excel backup download (restricted to downloadable roles)
- **Weekly backup reminder** for admins
- **Restore from a backup file** — upload the downloaded Excel and replace all data (accident recovery)

---

## 1. Quick start (local)

```bash
# 1. clone the repo
git clone https://github.com/PLJKT/familyfinancials.git
cd familyfinancials

# 2. create a virtual environment
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 3. install dependencies
pip install -r requirements.txt

# 4. run
uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000>.

### Default master admin
| Field | Value |
|-------|-------|
| Username | `admin` |
| Password | `admin123` |

> **Change the password immediately after first login** (or set `MASTER_PASSWORD` env var before first run).

---

## 2. Accounts and roles

**Self‑registration is disabled.** The login page has no “Register” tab: an administrator creates
every account in **Admin → Add user** (username, email, password, role) and the person can sign in
immediately. Admins can also set a new password for any user from the same page.

| Role | Permissions |
|------|-------------|
| `master_admin` | Everything, including assigning roles and resetting admin passwords. |
| `admin` | Manage users, full data access, backup download, restore. |
| `editor` | Add / edit / delete transactions and categories, download files. |
| `viewer` | Read‑only access to dashboard, transactions, reports. |
| `downloader` | Read‑only **plus** CSV / Excel download. |

Rules enforced by the API:

- capabilities are cumulative: `editor` includes everything `downloader` can do, `admin` includes everything `editor` can do, and `viewer` is the only strictly read-only role;
- only the master admin can create an `admin` (or change roles);
- nobody can create another `master_admin` through the API;
- only the master admin can reset an `admin`/`master_admin` password;
- only the master admin can delete an account, and never their own.

> **Storage warning:** when the app runs without a persistent database (`DATABASE_URL` unset →
> local SQLite file) admins see a red banner explaining that hosted free tiers erase the filesystem
> on every redeploy/spin‑down. Fix it by pointing `DATABASE_URL` at a real PostgreSQL database.

### If a deploy fails right after adding `DATABASE_URL`

The app prints one password‑free line saying what went wrong, e.g.
`DATABASE CONNECTION FAILED (attempt 5/5) postgresql://user:***@host/db | OperationalError: ...`
followed by `RuntimeError: Could not connect to the database. backend=postgresql host=... user=...`.
Most common causes:

1. the value carries quotes or a leading `psql ` — it must begin exactly with `postgresql://`;
2. the password is missing or still the literal `<password>` placeholder;
3. the variable was added to a different service, or was never saved (Render applies it on the next
   deploy only);
4. the database refuses the connection (suspended project, IP allow‑list, wrong host).

`GET /healthz` answers the same question live: `"backend":"sqlite"` means `DATABASE_URL` never
reached the app, while `"database_ok":false` returns the exact driver error.

---

## 3. Accounts, savings and financial statements

### The model in one minute

Money lives in **accounts** — a cash account and a savings account — and each account starts from an
**opening balance**: the money the family already held when the records begin. An opening balance is a
*stock*: it is never income, an expense or a saving. On top of it there are four kinds of movement:

| Movement | What it does | Net worth |
|----------|--------------|-----------|
| **Income** | cash up | up |
| **Expenses** | cash down | down |
| **Transfer**: *savings* (cash → savings) and *withdrawal* (savings → cash) | moves money between your own accounts | unchanged |
| **Loan**: `borrow` (cash up, debt up) / `repay` (cash down, debt down) | cash and debt move together | unchanged |

which gives the identity the whole system rests on:

```
net worth     = opening balances + (income − expenses)
income − expenses = Δcash + Δsavings + Δother assets − Δdebt
```

The balance sheet prints that check (`opening balances + lifetime surplus = net worth`), so a missing
entry shows up immediately — and the **Reconciliation** tab verifies it month by month.

### The month-end sweep (automatic)

Once a month has ended the system posts one automatic movement so the month's surplus lands in savings:

```
sweep = surplus − savings you recorded yourself that month + your withdrawals
sweep > 0  ->  a transfer INTO savings
sweep < 0  ->  a WITHDRAWAL (the month ran a deficit, funded from savings)
```

- Transfers you record yourself count towards it; transfers **funded by a loan** are excluded, so a
  loan used to top up savings is not cancelled out again.
- It is idempotent — one row per closed month, matched by its month and updated in place — and it
  re-runs on every startup and after every change to transactions, savings, transfers or a restore.
- The **running month is left alone** until it ends; until then its surplus sits in cash.
- Admins can re-run it by hand: **Savings → Refresh month-end sweep** (`POST /api/admin/sweep/run`).

### Recording money movements

- **Savings → Add saving** — a saving out of income (date, amount, whose saving, note).
- **Savings → Transfer / withdraw** — move money between cash and savings, choosing what funded it
  (`income`, `loan`, `earlier savings`, `other`) so the sweep classifies it correctly.
- **Statements → Accounts, loans & items → Record borrow / repay** — borrowing and repayments per
  lender; the outstanding balance maintains itself. Interest is a normal expense entry.
- **Transactions → Add transaction** — income, expenses or a saving, with the family member attached.
- **Statements → Accounts, loans & items → Add account** — add another account or set/correct an
  opening balance (this is where savings accumulated before the records start belong).

### Income statement (`Statements → Income statement`)

Income by category → total income; expenses with group subtotals → total expenses; then the surplus.
Below the line come the money movements that do not change net worth: transfers in (with the
loan-funded part called out), withdrawals, net movement in savings, borrowing and repayments.

### Balance sheet (`Statements → Balance sheet`)

```
ASSETS
  Cash account        opening + in − out = balance
  Savings account     opening + in − out = balance
  --- money in accounts ---
  Property / vehicle / investment / other (entered by hand)
  TOTAL ASSETS
LIABILITIES
  Loans (outstanding = borrowed − repaid, per lender)
  Other debts entered by hand
  TOTAL LIABILITIES
NET WORTH = TOTAL ASSETS − TOTAL LIABILITIES
```

Datable (*as of* any day), printable (**Print / save as PDF**), and it shows the identity check at the
bottom.

### Reconciliation (`Statements → Reconciliation`)

Month by month: income, expenses, surplus, money into and out of savings, borrowings, repayments and
the resulting cash balance — with a **check** badge on any month whose deficit has no recorded source
(usually missing income, or a movement nobody entered yet). A clean sheet means every surplus reached
savings and every gap has a source.

---

## 4. Backup, weekly reminder, and restore

### Weekly reminder
The Admin page (and a banner on every page) reminds admins to download an Excel backup every
`BACKUP_REMINDER_DAYS` days (default **7**). Every download is logged, so the reminder resets as soon
as a backup is taken. The banner also lists the recent backup/restore history.

### The backup file
`Download Excel backup` produces `family_finance_backup_YYYYMMDD_HHMM.xlsx` with three sheets:

| Sheet | Contents |
|-------|----------|
| `Transactions` | `Date, Type, Category, Amount, Description, Member, AutoOffset` — one row per transaction |
| `Categories` | `Category, Type, Group, Description` — keeps category groups on a restore |
| `Read me` | Human instructions |

`Member` records which family member a saving belongs to and `AutoOffset` holds the month
(`YYYY-MM`) of an automatic sweep row; both are optional — older files without them still import,
and files you hand-edit only need `Date, Type, Category, Amount`.

The same data is available as CSV (`Download CSV`).

### Restore (accident recovery)
**Admin → Backup & restore → Restore** uploads a backup and **replaces all existing transactions**:

1. the file is parsed and validated **before** anything is written — a bad file changes nothing;
2. the whole replace runs in a single database transaction (all or nothing);
3. unknown category names in the file are created automatically;
4. malformed rows are reported; the import is refused outright if more than 5 % of rows are unreadable;
5. the request must send `confirm=REPLACE_ALL`, and the UI additionally asks for a tick‑box confirmation;
6. the UI automatically downloads a copy of the *current* data before uploading, just in case.

Accepted inputs: `.xlsx`, `.xlsm`, `.csv`, with the columns above (column order and letter case do not
matter, an extra `ID` column is ignored, `Income`/`Expenses`/`Savings` accept a few aliases, dates are
`YYYY-MM-DD`, amounts may contain thousand separators).

---

## 5. Deploy to Render.com (free tier)

1. Push this repository to GitHub.
2. On [Render](https://render.com) click **New → Blueprint** and connect the repo.
   Render reads `render.yaml` and creates:
   - a free PostgreSQL database (`familyfinancials-db`)
   - a free web service (`familyfinancials`)
3. During creation Render asks for `MASTER_PASSWORD` (marked `sync: false`) — enter a strong password.
4. After deploy, open the service URL and log in with `admin` / your chosen password.

> The app reads `DATABASE_URL` from the environment; without it, it falls back to a local SQLite file.
> **Render's free instances have an ephemeral filesystem**, so make sure `DATABASE_URL` is set (the
> Blueprint does this) — otherwise every spin‑down or deploy erases data entered through the app.

---

## 6. Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `DATABASE_URL` | `sqlite:///./familyfinancials.db` | Database connection string. |
| `SECRET_KEY` | `change-me-in-production-please-use-env` | JWT signing key. **Set this to a long random string in production.** |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `10080` (7 days) | JWT lifetime. |
| `MASTER_USERNAME` | `admin` | Master admin username created on first run. |
| `MASTER_EMAIL` | `admin@example.com` | Master admin email. |
| `MASTER_PASSWORD` | `admin123` | Master admin password (only used when the account is first created). |
| `BACKUP_REMINDER_DAYS` | `7` | How often admins are reminded to download a backup. |

---

## 7. Seed data

The repository includes pre‑prepared seed data in `data/`:

- `data/seed_categories.csv` – the 21 categories from the revised workbook.
- `data/seed_transactions.csv` – all 887 transactions (date, type, category, amount, description).

On first startup the app seeds these automatically if the database is empty.

---

## 8. API overview

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/auth/login` | Obtain a JWT. |
| `POST` | `/api/auth/register` | **Disabled** (403) — accounts are created by admins. |
| `GET`  | `/api/auth/me` | Current user profile. |
| `GET`  | `/api/users` | List users (admin+). |
| `POST` | `/api/users` | **Create a user account** (admin+; `admin` role only by master). |
| `POST` | `/api/users/{id}/password` | Set a new password for a user (admin+). |
| `DELETE` | `/api/users/{id}` | Delete a family member's account (master only). |
| `PATCH` | `/api/users/{id}` | Update role / approval / active flag (admin+). |
| `GET`  | `/api/categories` | List categories. |
| `POST` | `/api/categories` | Create category (editor+). |
| `GET`  | `/api/transactions` | List transactions with filters. |
| `POST` | `/api/transactions` | Create transaction (editor+). |
| `PUT`  | `/api/transactions/{id}` | Update transaction (editor+). |
| `DELETE` | `/api/transactions/{id}` | Delete transaction (editor+). |
| `POST` | `/api/reports/summary` | Aggregated report (`group_by` = month/year/category). |
| `GET`  | `/api/accounts` | Accounts with opening balance, movements and balance. |
| `POST` | `/api/accounts` · `PUT`/`DELETE` `/api/accounts/{id}` | Add / edit / remove an account (editor+). |
| `POST` | `/api/transfers` | Move money between cash and savings (`direction` in/out, editor+). |
| `POST` | `/api/loans` | Record borrowing or a repayment (editor+). |
| `GET`  | `/api/loans` | Outstanding balance per lender. |
| `GET`  | `/api/reconciliation` | Month-by-month reconciliation with gap flags. |
| `GET`  | `/api/savings/summary` | Savings per month **and per family member** + sweep status. |
| `POST` | `/api/savings/entries` | Record a saving (editor+). |
| `POST` | `/api/admin/sweep/run` | Re-apply the automatic month-end sweep (admin+). |
| `POST` | `/api/admin/migrate-accounts-model` | One-off data migration (master; dry run by default). |
| `GET`  | `/api/statements/income` | Income statement for a period (`start`, `end`). |
| `GET`  | `/api/statements/balance-sheet` | Balance sheet (`as_of`). |
| `GET`  | `/api/assets` · `/api/liabilities` | Balance-sheet items (list for any user; add/update/delete editor+). |
| `GET`  | `/api/dashboard` | KPI data + 12‑month trend. |
| `GET`  | `/api/export/csv` | Download CSV backup (downloader+). |
| `GET`  | `/api/export/excel` | Download Excel backup (downloader+) — logs the weekly reminder. |
| `GET`  | `/api/admin/backup-status` | Reminder state + backup history (admin+). |
| `POST` | `/api/admin/import` | **Replace all transactions** from a backup file (admin+, needs `confirm=REPLACE_ALL`). |

Interactive docs: `/docs` (Swagger UI).

---

## 9. Security notes

- Passwords are hashed with **bcrypt** via `passlib`.
- JWTs are signed with `SECRET_KEY` — **always** set a strong value in production.
- There is no public sign‑up; only admins create accounts.
- Export and restore endpoints are protected by role guards; restore additionally requires an explicit
  confirmation field and is fully validated before writing.
- For production prefer PostgreSQL and keep `SECRET_KEY` / `MASTER_PASSWORD` in the host's environment
  variables, never in the repo.

---

## 10. Project structure

```
familyfinancials/
├── app/
│   ├── main.py        # FastAPI routes
│   ├── models.py      # SQLAlchemy models (User, Category, Transaction, AssetItem, ...)
│   ├── finance.py     # month-end saving sweep, savings summary, statements
│   ├── migrate.py     # adds new columns to an existing database on startup
│   ├── schemas.py     # Pydantic schemas
│   ├── crud.py        # business logic, reports, restore, backup log
│   ├── backup_io.py   # export builders + backup file parser (round-trip safe)
│   ├── auth.py        # JWT, password hashing, role guards
│   ├── seed.py        # initial admin + data import
│   └── database.py    # engine / session
├── static/
│   ├── index.html     # single‑page UI
│   ├── css/style.css
│   └── js/app.js
├── data/
│   ├── seed_categories.csv
│   └── seed_transactions.csv
├── tests_admin_features.py   # end‑to‑end test of users, reminder, restore
├── tests_statements.py       # end‑to‑end test of savings, sweep, statements
├── tests_accounts_model.py   # accounts, opening balances, transfers, loans, migration
├── run_suites.sh             # runs every suite against its own fresh database
├── tests_live_full.py        # full check of a deployed instance
├── Dockerfile
├── render.yaml
├── requirements.txt
└── README.md
```

---

## 11. Running the tests

```bash
# start the app against a scratch database
DATABASE_URL="sqlite:///./test_import.db" uvicorn app.main:app --port 8002
# in another shell
python tests_admin_features.py
```

The suite covers account creation and role rules, the weekly reminder bookkeeping, Excel and CSV
round‑trips, hand‑edited files, and every safety rail of the restore endpoint.
