# Family Financial Control System

An online, login‑protected family budget control system built with **FastAPI + SQLite/PostgreSQL**.
It imports the existing **887 transactions** from the revised Excel workbook and provides:

- Secure login / registration (new accounts need master‑admin approval)
- Role‑based access: `master_admin`, `admin`, `editor`, `viewer`, `downloader`
- Add / edit / delete transactions with category dropdowns
- Automatic financial reports grouped by month, year, or category
- Dashboard with KPIs and charts (income, expenses, savings, balance)
- CSV / Excel export (restricted to downloadable roles)
- Responsive single‑page UI (Bootstrap 5 + Chart.js)

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

## 2. Roles

| Role | Permissions |
|------|-------------|
| `master_admin` | Everything, including approving users and assigning roles. |
| `admin` | Manage users (except changing roles), full data access, export. |
| `editor` | Add / edit / delete transactions and categories, export. |
| `viewer` | Read‑only access to dashboard, transactions, reports. |
| `downloader` | Read‑only **plus** CSV / Excel export. |

New registrations are created as `viewer` with `is_active = false` and `is_approved = false`.
The master admin approves them and assigns the appropriate role in the **Admin** page.

---

## 3. Deploy to Render.com (free tier)

1. Push this repository to GitHub.
2. On [Render](https://render.com) click **New → Blueprint** and connect the repo.
   Render will read `render.yaml` and create:
   - a free PostgreSQL database (`familyfinancials-db`)
   - a free web service (`familyfinancials`)
3. During creation Render asks for the value of `MASTER_PASSWORD` (marked `sync: false`).
   Enter a strong password for the `admin` account.
4. After deploy, open the service URL and log in with `admin` / your chosen password.

> The app reads `DATABASE_URL` from the environment. If it is absent it falls back to a local SQLite file.
> On Render the free PostgreSQL database is used automatically.

---

## 4. Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `DATABASE_URL` | `sqlite:///./familyfinancials.db` | Database connection string. |
| `SECRET_KEY` | `change-me-in-production-please-use-env` | JWT signing key. **Set this to a long random string in production.** |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `10080` (7 days) | JWT lifetime. |
| `MASTER_USERNAME` | `admin` | Master admin username created on first run. |
| `MASTER_EMAIL` | `admin@example.com` | Master admin email. |
| `MASTER_PASSWORD` | `admin123` | Master admin password (only used when the account is first created). |

---

## 5. Data import

The repository includes pre‑prepared seed data in `data/`:

- `data/seed_categories.csv` – the 21 categories from the revised workbook.
- `data/seed_transactions.csv` – all 887 transactions (date, type, category, amount, description).

On first startup the app seeds these automatically if the database is empty.
To re‑import from scratch, delete the SQLite file (or drop the tables) and restart.

---

## 6. API overview

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/auth/register` | Register a new user (pending approval). |
| `POST` | `/api/auth/login` | Obtain a JWT. |
| `GET`  | `/api/auth/me` | Current user profile. |
| `GET`  | `/api/categories` | List categories. |
| `POST` | `/api/categories` | Create category (editor+). |
| `GET`  | `/api/transactions` | List transactions with filters. |
| `POST` | `/api/transactions` | Create transaction (editor+). |
| `PUT`  | `/api/transactions/{id}` | Update transaction (editor+). |
| `DELETE` | `/api/transactions/{id}` | Delete transaction (editor+). |
| `POST` | `/api/reports/summary` | Aggregated report (`group_by` = month/year/category). |
| `GET`  | `/api/dashboard` | KPI data + 12‑month trend. |
| `GET`  | `/api/users` | List users (admin+). |
| `PATCH` | `/api/users/{id}` | Update role / approval / active flag (admin+). |
| `GET`  | `/api/export/csv` | Download CSV (downloader+). |
| `GET`  | `/api/export/excel` | Download Excel (downloader+). |

Interactive docs: `/docs` (Swagger UI).

---

## 7. Security notes

- Passwords are hashed with **bcrypt** via `passlib`.
- JWTs are signed with `SECRET_KEY` — **always** set a strong value in production.
- New accounts cannot log in until the master admin approves them.
- Export endpoints are protected by the `downloader` role.
- For production, prefer PostgreSQL (Render’s free tier) over SQLite, and store `SECRET_KEY` and `MASTER_PASSWORD` as Render environment variables, not in the repo.

---

## 8. Project structure

```
familyfinancials/
├── app/
│   ├── main.py        # FastAPI routes
│   ├── models.py      # SQLAlchemy models
│   ├── schemas.py     # Pydantic schemas
│   ├── crud.py        # business logic & reports
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
├── Dockerfile
├── render.yaml
├── requirements.txt
└── README.md
```
