# Family Financial Control System

An online, login‑protected family budget control system built with **FastAPI + SQLite/PostgreSQL**.
It ships with the existing **887 transactions** from the revised Excel workbook and provides:

- Secure login — **accounts are created by the administrator** (there is no public sign‑up)
- Role‑based access: `master_admin`, `admin`, `editor`, `viewer`, `downloader`
- Add / edit / delete transactions with category dropdowns
- Automatic financial reports grouped by month, year, or category
- Dashboard with KPIs and charts (income, expenses, savings, balance)
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

## 3. Backup, weekly reminder, and restore

### Weekly reminder
The Admin page (and a banner on every page) reminds admins to download an Excel backup every
`BACKUP_REMINDER_DAYS` days (default **7**). Every download is logged, so the reminder resets as soon
as a backup is taken. The banner also lists the recent backup/restore history.

### The backup file
`Download Excel backup` produces `family_finance_backup_YYYYMMDD_HHMM.xlsx` with three sheets:

| Sheet | Contents |
|-------|----------|
| `Transactions` | `Date, Type, Category, Amount, Description` — one row per transaction |
| `Categories` | `Category, Type, Group, Description` — keeps category groups on a restore |
| `Read me` | Human instructions |

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

## 4. Deploy to Render.com (free tier)

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

## 5. Environment variables

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

## 6. Seed data

The repository includes pre‑prepared seed data in `data/`:

- `data/seed_categories.csv` – the 21 categories from the revised workbook.
- `data/seed_transactions.csv` – all 887 transactions (date, type, category, amount, description).

On first startup the app seeds these automatically if the database is empty.

---

## 7. API overview

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
| `GET`  | `/api/dashboard` | KPI data + 12‑month trend. |
| `GET`  | `/api/export/csv` | Download CSV backup (downloader+). |
| `GET`  | `/api/export/excel` | Download Excel backup (downloader+) — logs the weekly reminder. |
| `GET`  | `/api/admin/backup-status` | Reminder state + backup history (admin+). |
| `POST` | `/api/admin/import` | **Replace all transactions** from a backup file (admin+, needs `confirm=REPLACE_ALL`). |

Interactive docs: `/docs` (Swagger UI).

---

## 8. Security notes

- Passwords are hashed with **bcrypt** via `passlib`.
- JWTs are signed with `SECRET_KEY` — **always** set a strong value in production.
- There is no public sign‑up; only admins create accounts.
- Export and restore endpoints are protected by role guards; restore additionally requires an explicit
  confirmation field and is fully validated before writing.
- For production prefer PostgreSQL and keep `SECRET_KEY` / `MASTER_PASSWORD` in the host's environment
  variables, never in the repo.

---

## 9. Project structure

```
familyfinancials/
├── app/
│   ├── main.py        # FastAPI routes
│   ├── models.py      # SQLAlchemy models (User, Category, Transaction, BackupLog)
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
├── Dockerfile
├── render.yaml
├── requirements.txt
└── README.md
```

---

## 10. Running the tests

```bash
# start the app against a scratch database
DATABASE_URL="sqlite:///./test_import.db" uvicorn app.main:app --port 8002
# in another shell
python tests_admin_features.py
```

The suite covers account creation and role rules, the weekly reminder bookkeeping, Excel and CSV
round‑trips, hand‑edited files, and every safety rail of the restore endpoint.
