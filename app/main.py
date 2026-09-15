import io
import csv
import logging
from datetime import date, datetime, timedelta
from typing import List, Optional

from fastapi import FastAPI, Depends, HTTPException, status, Query, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models, schemas, crud, auth, backup_io, finance
from .database import engine, get_db, SessionLocal, DATABASE_URL, safe_url, describe_url, BACKEND
from .migrate import ensure_schema
from .auth import (
    authenticate_user, create_access_token, get_current_user,
    require_roles, require_master, require_admin, require_editor, require_downloader,
    ROLE_MASTER, ROLE_ADMIN, ROLE_EDITOR, ROLE_VIEWER, ROLE_DOWNLOADER, ALL_ROLES,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("familyfinancials")

STARTED_AT = datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _database_check():
    """Return (reachable, error_text). Used by /healthz and the admin diagnostics."""
    from sqlalchemy import text

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, None
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


app = FastAPI(title="Family Financial Control System", version="1.4.0")


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static frontend
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.on_event("startup")
def on_startup():
    logger.info("Starting Family Financial Control System v%s | storage=%s | %s | persistent=%s | started_at=%s",
                app.version, BACKEND, describe_url(), not DATABASE_URL.startswith("sqlite"), STARTED_AT)
    try:
        # create_all() never adds columns to existing tables, so patch first
        models.Base.metadata.create_all(bind=engine)
        ensure_schema(engine)
        from .seed import seed_initial_data
        seed_initial_data()
        with SessionLocal() as db:
            started = finance.ensure_default_accounts(db)
            if started:
                logger.info("Created the default accounts: %s", ", ".join(started))
            sweep = finance.apply_month_end_sweep(db)
        logger.info("Month-end sweep: %s created, %s updated, %s removed (%s closed months)",
                    sweep["created"], sweep["updated"], sweep["removed"], len(sweep["closed_months"]))
    except Exception:
        logger.exception("STARTUP FAILED while preparing the database at %s", safe_url())
        raise
    logger.info("Startup complete - the app is ready to serve requests.")


@app.get("/healthz")
def healthz():
    """Public liveness probe: no configuration details, safe to expose."""
    db_ok, _ = _database_check()
    return {
        "status": "ok" if db_ok else "degraded",
        "database_ok": db_ok,
        "backend": BACKEND,
        "storage_persistent": not DATABASE_URL.startswith("sqlite"),
        "version": app.version,
        "started_at": STARTED_AT,
    }


# ---------------- Frontend ----------------
@app.get("/")
def root():
    return FileResponse("static/index.html")


# ---------------- Auth ----------------
@app.post("/api/auth/register", status_code=status.HTTP_403_FORBIDDEN)
def register_disabled():
    """Self-registration is disabled: accounts are created by an administrator."""
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Self-registration is disabled. Please ask the administrator to create your account.",
    )


@app.post("/api/auth/login", response_model=schemas.Token)
def login(form_data: schemas.LoginRequest, db: Session = Depends(get_db)):
    user = authenticate_user(db, form_data.username, form_data.password)
    if not user:
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    if not user.is_active or not user.is_approved:
        raise HTTPException(status_code=403, detail="Account pending approval by the master admin")
    token = create_access_token(data={"sub": user.username, "role": user.role})
    return schemas.Token(access_token=token, token_type="bearer", user=user)


@app.get("/api/auth/me", response_model=schemas.UserOut)
def read_me(current_user: models.User = Depends(get_current_user)):
    return current_user


# ---------------- Users (admin) ----------------
CREATABLE_ROLES = [ROLE_ADMIN, ROLE_EDITOR, ROLE_VIEWER, ROLE_DOWNLOADER]


@app.get("/api/users", response_model=List[schemas.UserOut])
def list_users(db: Session = Depends(get_db), current_user: models.User = Depends(require_admin)):
    return crud.list_users(db)


@app.post("/api/users", response_model=schemas.UserOut, status_code=status.HTTP_201_CREATED)
def create_user(user: schemas.UserAdminCreate, db: Session = Depends(get_db),
                current_user: models.User = Depends(require_admin)):
    """Admins create family accounts directly – no public registration exists."""
    if user.role not in CREATABLE_ROLES:
        raise HTTPException(status_code=400,
                            detail="Role must be one of: " + ", ".join(CREATABLE_ROLES))
    if user.role == ROLE_ADMIN and current_user.role != ROLE_MASTER:
        raise HTTPException(status_code=403, detail="Only the master admin can create admins")
    if crud.get_user_by_username(db, user.username):
        raise HTTPException(status_code=400, detail="Username already exists")
    if crud.get_user_by_email(db, user.email):
        raise HTTPException(status_code=400, detail="Email already exists")
    db_user = crud.create_user(db, schemas.UserCreate(**user.model_dump(
        include={"username", "email", "full_name", "password"})),
        role=user.role, is_active=user.is_active, is_approved=user.is_approved)
    return db_user


@app.post("/api/users/{user_id}/password", response_model=schemas.UserOut)
def reset_password(user_id: int, data: schemas.PasswordReset, db: Session = Depends(get_db),
                   current_user: models.User = Depends(require_admin)):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role in (ROLE_MASTER, ROLE_ADMIN) and current_user.role != ROLE_MASTER:
        raise HTTPException(status_code=403,
                            detail="Only the master admin can reset an admin password")
    user.hashed_password = auth.get_password_hash(data.new_password)
    db.commit()
    db.refresh(user)
    return user


@app.delete("/api/users/{user_id}")
def delete_user(user_id: int, db: Session = Depends(get_db),
                current_user: models.User = Depends(require_master)):
    """Remove a family member's account (master admin only)."""
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="You cannot delete your own account")
    if user.role == ROLE_MASTER:
        raise HTTPException(status_code=400, detail="The master admin account cannot be deleted")

    username = user.username
    # keep history intact: detach rows that reference the user
    db.query(models.Transaction).filter(models.Transaction.created_by == user_id).update(
        {models.Transaction.created_by: None}, synchronize_session=False)
    db.query(models.BackupLog).filter(models.BackupLog.user_id == user_id).update(
        {models.BackupLog.user_id: None}, synchronize_session=False)
    db.delete(user)
    db.commit()
    return {"ok": True, "deleted": username}


@app.patch("/api/users/{user_id}", response_model=schemas.UserOut)
def update_user(user_id: int, data: schemas.UserUpdate, db: Session = Depends(get_db),
                current_user: models.User = Depends(require_admin)):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    # Only master admin can change roles / approve admins
    if data.role is not None and current_user.role != ROLE_MASTER:
        raise HTTPException(status_code=403, detail="Only the master admin can change roles")
    if data.role is not None and data.role not in ALL_ROLES:
        raise HTTPException(status_code=400, detail="Invalid role")
    return crud.update_user(db, user, data)


# ---------------- Categories ----------------
@app.get("/api/categories", response_model=List[schemas.CategoryOut])
def get_categories(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    return crud.list_categories(db)


@app.post("/api/categories", response_model=schemas.CategoryOut)
def add_category(data: schemas.CategoryCreate, db: Session = Depends(get_db),
                 current_user: models.User = Depends(require_editor)):
    return crud.create_category(db, data)


@app.put("/api/categories/{category_id}", response_model=schemas.CategoryOut)
def edit_category(category_id: int, data: schemas.CategoryBase, db: Session = Depends(get_db),
                  current_user: models.User = Depends(require_editor)):
    cat = crud.get_category(db, category_id)
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")
    return crud.update_category(db, cat, data)


@app.delete("/api/categories/{category_id}")
def remove_category(category_id: int, db: Session = Depends(get_db),
                    current_user: models.User = Depends(require_admin)):
    cat = crud.get_category(db, category_id)
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")
    crud.delete_category(db, cat)
    return {"ok": True}


# ---------------- Transactions ----------------
@app.get("/api/transactions", response_model=List[schemas.TransactionOut])
def get_transactions(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    category_ids: Optional[List[int]] = Query(None),
    types: Optional[List[str]] = Query(None),
    limit: int = 1000,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    return crud.list_transactions(db, start_date, end_date, category_ids, types, limit, offset)


@app.post("/api/transactions", response_model=schemas.TransactionOut)
def add_transaction(data: schemas.TransactionCreate, db: Session = Depends(get_db),
                    current_user: models.User = Depends(require_editor)):
    if not crud.get_category(db, data.category_id):
        raise HTTPException(status_code=400, detail="Invalid category")
    if data.member_id and not db.query(models.User).filter(models.User.id == data.member_id).first():
        raise HTTPException(status_code=400, detail="Unknown family member")
    trx = crud.create_transaction(db, data, current_user.id)
    _refresh_offsets(db)          # keep the month-end sweep in step with the data
    db.refresh(trx)
    return trx


@app.put("/api/transactions/{trx_id}", response_model=schemas.TransactionOut)
def edit_transaction(trx_id: int, data: schemas.TransactionUpdate, db: Session = Depends(get_db),
                     current_user: models.User = Depends(require_editor)):
    trx = db.query(models.Transaction).filter(models.Transaction.id == trx_id).first()
    if not trx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    trx = crud.update_transaction(db, trx, data)
    _refresh_offsets(db)
    db.refresh(trx)
    return trx


@app.delete("/api/transactions/{trx_id}")
def remove_transaction(trx_id: int, db: Session = Depends(get_db),
                       current_user: models.User = Depends(require_editor)):
    trx = db.query(models.Transaction).filter(models.Transaction.id == trx_id).first()
    if not trx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    crud.delete_transaction(db, trx)
    _refresh_offsets(db)
    return {"ok": True}


# ---------------- Reports ----------------
@app.post("/api/reports/summary", response_model=schemas.SummaryResponse)
def report_summary(query: schemas.ReportQuery, db: Session = Depends(get_db),
                   current_user: models.User = Depends(get_current_user)):
    return crud.build_summary(db, query)


@app.get("/api/dashboard")
def dashboard(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    return crud.dashboard_kpis(db)


# ---------------- Savings ----------------
def _refresh_offsets(db: Session) -> dict:
    """Re-apply the automatic month-end sweep; never breaks the caller on failure."""
    try:
        return finance.apply_month_end_sweep(db)
    except Exception:
        logger.exception("The month-end sweep could not be refreshed")
        db.rollback()
        return {}


@app.get("/api/savings/summary")
def savings_summary(months: int = 12, db: Session = Depends(get_db),
                    current_user: models.User = Depends(get_current_user)):
    """Month-by-month savings per family member, with the automatic sweep status."""
    return finance.savings_summary(db, months=months)


@app.post("/api/savings/entries", response_model=schemas.TransactionOut)
def add_saving(data: schemas.SavingsEntryCreate, db: Session = Depends(get_db),
               current_user: models.User = Depends(require_editor)):
    """Record a saving contribution (stored as a Savings transfer into the account)."""
    if data.amount <= 0:
        raise HTTPException(status_code=400, detail="The saving amount must be greater than zero")

    category_id = data.category_id
    if category_id is None:
        cat = (db.query(models.Category)
               .filter(models.Category.name == finance.SAVING_CATEGORY_NAME).first()
               or db.query(models.Category).filter(models.Category.type == "Savings").first())
        if cat is None:
            raise HTTPException(status_code=400, detail="No Savings category exists yet")
        category_id = cat.id
    if not crud.get_category(db, category_id):
        raise HTTPException(status_code=400, detail="Invalid category")

    if data.member_id and not db.query(models.User).filter(models.User.id == data.member_id).first():
        raise HTTPException(status_code=400, detail="Unknown family member")

    payload = schemas.TransactionCreate(
        date=data.date, type=models.TYPE_SAVINGS, category_id=category_id, amount=float(data.amount),
        description=data.note or "Saving contribution",
        member_id=data.member_id or current_user.id,
        account_id=data.account_id,
        funded_by=data.funded_by or "income",
    )
    trx = crud.create_transaction(db, payload, current_user.id)
    _refresh_offsets(db)
    db.refresh(trx)
    return trx


@app.post("/api/admin/sweep/run", response_model=schemas.SweepResult)
def run_sweep(db: Session = Depends(get_db),
              current_user: models.User = Depends(require_admin)):
    """Apply / refresh the automatic month-end sweep (idempotent)."""
    return _refresh_offsets(db)


@app.post("/api/admin/offsets/run", response_model=schemas.SweepResult)
def run_offsets_legacy(db: Session = Depends(get_db),
                       current_user: models.User = Depends(require_admin)):
    """Same as /api/admin/sweep/run — kept for older clients."""
    return _refresh_offsets(db)


# ---------------- Accounts ----------------
@app.get("/api/accounts", response_model=List[schemas.AccountOut])
def get_accounts(include_inactive: bool = False, db: Session = Depends(get_db),
                 current_user: models.User = Depends(get_current_user)):
    return finance.list_accounts(db, include_inactive)


@app.post("/api/accounts", response_model=schemas.AccountOut, status_code=status.HTTP_201_CREATED)
def create_account(data: schemas.AccountCreate, db: Session = Depends(get_db),
                   current_user: models.User = Depends(require_editor)):
    if data.kind not in models.ACCOUNT_KINDS:
        raise HTTPException(status_code=400,
                            detail=f"kind must be one of: {', '.join(models.ACCOUNT_KINDS)}")
    if db.query(models.Account).filter(func.lower(models.Account.name) == data.name.strip().lower()).first():
        raise HTTPException(status_code=400, detail="An account with that name already exists")
    account = models.Account(**data.model_dump(), created_by=current_user.id)
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


@app.put("/api/accounts/{account_id}", response_model=schemas.AccountOut)
def update_account(account_id: int, data: schemas.AccountUpdate, db: Session = Depends(get_db),
                   current_user: models.User = Depends(require_editor)):
    """Opening balances are edited here — they are stocks, never income."""
    account = db.query(models.Account).filter(models.Account.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    values = data.model_dump(exclude_unset=True)
    if values.get("kind") and values["kind"] not in models.ACCOUNT_KINDS:
        raise HTTPException(status_code=400,
                            detail=f"kind must be one of: {', '.join(models.ACCOUNT_KINDS)}")
    for field, value in values.items():
        setattr(account, field, value)
    db.commit()
    db.refresh(account)
    return account


@app.delete("/api/accounts/{account_id}")
def delete_account(account_id: int, db: Session = Depends(get_db),
                   current_user: models.User = Depends(require_editor)):
    account = db.query(models.Account).filter(models.Account.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    used = (db.query(func.count(models.Transaction.id))
            .filter(models.Transaction.account_id == account_id).scalar() or 0)
    if used:
        raise HTTPException(status_code=400,
                            detail=f"{used} transaction(s) use this account — deactivate it instead")
    db.delete(account)
    db.commit()
    return {"ok": True}


# ---------------- Transfers and loans ----------------
@app.post("/api/transfers", response_model=schemas.TransactionOut)
def add_transfer(data: schemas.TransferEntryCreate, db: Session = Depends(get_db),
                 current_user: models.User = Depends(require_editor)):
    """Move money between the family's own accounts (cash <-> savings)."""
    if data.amount <= 0:
        raise HTTPException(status_code=400, detail="The amount must be greater than zero")
    if data.direction not in ("in", "out"):
        raise HTTPException(status_code=400, detail="direction must be 'in' or 'out'")
    cat = (db.query(models.Category)
           .filter(models.Category.name == finance.SAVING_CATEGORY_NAME).first()
           or db.query(models.Category).filter(models.Category.type == "Savings").first())
    if cat is None:
        raise HTTPException(status_code=400, detail="No Savings category exists yet")
    if data.funded_by and data.funded_by not in models.FUNDED_BY:
        raise HTTPException(status_code=400,
                            detail=f"funded_by must be one of: {', '.join(models.FUNDED_BY)}")
    payload = schemas.TransactionCreate(
        date=data.date,
        type=models.TYPE_SAVINGS if data.direction == "in" else models.TYPE_WITHDRAWAL,
        category_id=cat.id, amount=float(data.amount),
        description=data.note or ("Transfer into savings" if data.direction == "in"
                                  else "Withdrawal from savings"),
        member_id=data.member_id,
        account_id=data.account_id,
        funded_by=data.funded_by if data.direction == "in" else None,
    )
    trx = crud.create_transaction(db, payload, current_user.id)
    _refresh_offsets(db)
    db.refresh(trx)
    return trx


@app.post("/api/loans", response_model=schemas.TransactionOut)
def add_loan(data: schemas.LoanEntryCreate, db: Session = Depends(get_db),
             current_user: models.User = Depends(require_editor)):
    """Record borrowing (cash up, debt up) or a repayment (cash down, debt down)."""
    if data.amount <= 0:
        raise HTTPException(status_code=400, detail="The amount must be greater than zero")
    if data.direction not in ("borrow", "repay"):
        raise HTTPException(status_code=400, detail="direction must be 'borrow' or 'repay'")
    if not (data.lender or "").strip():
        raise HTTPException(status_code=400, detail="A lender name is required")
    cat = db.query(models.Category).filter(models.Category.name == "Loan").first()
    if cat is None:
        cat = models.Category(name="Loan", type="Loan", group="借款",
                              description="Borrowing and repayment — never income or expense")
        db.add(cat)
        db.commit()
        db.refresh(cat)
    payload = schemas.TransactionCreate(
        date=data.date, type=models.TYPE_LOAN, category_id=cat.id, amount=float(data.amount),
        description=data.note or (f"Borrowed from {data.lender}" if data.direction == "borrow"
                                  else f"Repaid to {data.lender}"),
        account_id=data.account_id,
        direction=data.direction, lender=data.lender.strip(),
    )
    trx = crud.create_transaction(db, payload, current_user.id)
    db.refresh(trx)
    return trx


@app.get("/api/loans", response_model=List[schemas.LoanBalanceOut])
def get_loans(db: Session = Depends(get_db),
              current_user: models.User = Depends(get_current_user)):
    """Outstanding balance per lender: borrowed minus repaid."""
    return finance.loan_balances(db)["loans"]


# ---------------- Reconciliation ----------------
@app.get("/api/reconciliation")
def get_reconciliation(start: Optional[date] = None, end: Optional[date] = None,
                       db: Session = Depends(get_db),
                       current_user: models.User = Depends(get_current_user)):
    """Month by month: does the money add up, and was every deficit funded?"""
    return finance.reconciliation(db, start, end)


# ---------------- One-off move to the accounts model ----------------
@app.post("/api/admin/migrate-accounts-model", response_model=schemas.ModelMigrationResult)
def migrate_accounts_model(
    apply: bool = False,
    opening_savings: float = 0.0,
    opening_savings_date: Optional[date] = None,
    loan_amount: float = 0.0,
    loan_lender: str = "Company",
    loan_date: Optional[date] = None,
    loan_funded_savings_amount: float = 0.0,
    loan_funded_savings_date: Optional[date] = None,
    remove_static_assets: List[str] = Query(default=[]),
    remove_static_liabilities: List[str] = Query(default=[]),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_master),
):
    """Move existing data onto the accounts model (master admin only).

    Dry run by default — call with ``apply=true`` to write. Idempotent: a second
    run reports ``already_applied`` and changes nothing. It

      1. creates the cash/savings accounts and sets the savings opening balance,
      2. reclassifies negative automatic saving rows as Withdrawals,
      3. marks the loan-funded transfer as funded by the loan,
      4. records the borrowing as a Loan row (cash up, debt up),
      5. removes the static asset/liability pairs it replaces,
      6. re-runs the month-end sweep so the figures are recomputed.
    """
    report = schemas.ModelMigrationResult(ok=True, already_applied=False)
    before = finance.balance_sheet(db)
    report.before = {
        "savings": before["savings_total"], "cash": before["cash_total"],
        "loans": before["loans_total"], "net_worth": before["net_worth"],
        "liabilities": before["liabilities_total"], "assets": before["asset_items_total"],
    }

    accounts = finance.list_accounts(db)
    savings_account = next((a for a in accounts if a.kind == "savings"), None)
    cash_account = next((a for a in accounts if a.kind == "cash"), None)
    if savings_account is None or cash_account is None:
        if apply:
            created = finance.ensure_default_accounts(db)
            report.accounts_created = created
            accounts = finance.list_accounts(db)
            savings_account = next((a for a in accounts if a.kind == "savings"), None)
            cash_account = next((a for a in accounts if a.kind == "cash"), None)
        else:
            report.accounts_created = ["Cash", "Savings"]

    if opening_savings and savings_account is not None:
        if abs(float(savings_account.opening_balance or 0.0)) < 0.005:
            report.opening_balances_set.append(
                f"{savings_account.name}: {opening_savings:,.0f}"
                + (f" as of {opening_savings_date}" if opening_savings_date else ""))
            if apply:
                savings_account.opening_balance = float(opening_savings)
                savings_account.opening_date = opening_savings_date
                savings_account.note = (savings_account.note or "") or \
                    "Balance accumulated over the years before the records start"
                db.commit()
        else:
            report.notes.append(
                f"{savings_account.name} already has an opening balance of "
                f"{float(savings_account.opening_balance):,.0f} — left unchanged")

    # 2. negative automatic rows become withdrawals
    negatives = (db.query(models.Transaction)
                 .filter(models.Transaction.auto_offset_month.isnot(None),
                         models.Transaction.type == models.TYPE_SAVINGS,
                         models.Transaction.amount < 0).all())
    report.withdrawals_reclassified = len(negatives)
    if apply:
        for row in negatives:
            row.type = models.TYPE_WITHDRAWAL
            row.amount = abs(float(row.amount))
            row.description = (f"Month-end withdrawal from savings for "
                               f"{row.auto_offset_month} (the month ran a deficit)")
        db.commit()

    # 3. the transfer that was funded by the loan
    if loan_funded_savings_amount:
        candidates = (db.query(models.Transaction)
                      .filter(models.Transaction.type == models.TYPE_SAVINGS,
                              models.Transaction.auto_offset_month.is_(None)).all())
        match = None
        for row in candidates:
            same_amount = abs(float(row.amount) - loan_funded_savings_amount) < 0.01
            same_date = (not loan_funded_savings_date) or row.date == loan_funded_savings_date
            if same_amount and same_date and row.funded_by != "loan":
                match = row
                break
        if match is not None:
            report.savings_reclassified = 1
            report.notes.append(
                f"{match.amount:,.0f} on {match.date} marked as funded by the loan")
            if apply:
                match.funded_by = "loan"
                if not match.description:
                    match.description = "Transferred from the loan proceeds into savings"
                db.commit()
        else:
            already = any(abs(float(r.amount) - loan_funded_savings_amount) < 0.01
                          and r.funded_by == "loan" for r in candidates)
            report.notes.append("loan-funded transfer already marked" if already
                                else "no matching transfer found to mark as loan-funded")

    # 4. record the borrowing itself
    if loan_amount:
        existing_loan = (db.query(models.Transaction)
                         .filter(models.Transaction.type == models.TYPE_LOAN).first())
        if existing_loan is None:
            report.loan_rows_created = 1
            report.notes.append(f"loan of {loan_amount:,.0f} from {loan_lender} will be recorded "
                                f"on {loan_date or 'its original date'}")
            if apply:
                category = db.query(models.Category).filter(models.Category.name == "Loan").first()
                if category is None:
                    category = models.Category(name="Loan", type="Loan", group="借款",
                                               description="Borrowing and repayment — never income")
                    db.add(category)
                    db.commit()
                    db.refresh(category)
                db.add(models.Transaction(
                    date=loan_date or date.today(), type=models.TYPE_LOAN, category_id=category.id,
                    amount=float(loan_amount), direction="borrow", lender=loan_lender,
                    account_id=cash_account.id if cash_account else None,
                    description=f"Borrowed from {loan_lender}"),
                )
                db.commit()
        else:
            report.notes.append("a loan is already recorded in the ledger")

    # 5. the static items the ledger replaces
    for name in remove_static_assets:
        item = db.query(models.AssetItem).filter(models.AssetItem.name == name).first()
        if item is not None:
            report.static_items_removed.append(f"asset: {name}")
            if apply:
                db.delete(item)
    for name in remove_static_liabilities:
        item = db.query(models.LiabilityItem).filter(models.LiabilityItem.name == name).first()
        if item is not None:
            report.static_items_removed.append(f"liability: {name}")
            if apply:
                db.delete(item)
    if apply and report.static_items_removed:
        db.commit()

    # 6. recompute the sweep
    sweep = _refresh_offsets(db)
    report.sweep_rows_fixed = int((sweep or {}).get("updated", 0))
    if apply and not any([report.accounts_created, report.opening_balances_set,
                          report.withdrawals_reclassified, report.savings_reclassified,
                          report.loan_rows_created, report.static_items_removed,
                          report.sweep_rows_fixed]):
        report.already_applied = True

    after = finance.balance_sheet(db)
    report.after = {
        "savings": after["savings_total"], "cash": after["cash_total"],
        "loans": after["loans_total"], "net_worth": after["net_worth"],
        "liabilities": after["liabilities_total"], "assets": after["asset_items_total"],
        "accounts": [{"name": a["name"], "kind": a["kind"], "opening": a["opening_balance"],
                      "movements_in": a["movements_in"], "movements_out": a["movements_out"],
                      "balance": a["balance"]} for a in after["accounts"]],
        "reconciliation_difference": after["reconciliation"]["difference"],
    }
    report.notes.append(f"sweep: {sweep.get('created', 0)} created, {sweep.get('updated', 0)} updated")
    if not apply:
        report.notes.append("DRY RUN — nothing was written; call again with apply=true")
    return report


# ---------------- Financial statements ----------------
@app.get("/api/statements/income")
def statement_income(start: Optional[date] = None, end: Optional[date] = None,
                     db: Session = Depends(get_db),
                     current_user: models.User = Depends(get_current_user)):
    """Income statement: income, expenses (with group subtotals) and savings."""
    return finance.income_statement(db, start, end)


@app.get("/api/statements/balance-sheet")
def statement_balance_sheet(as_of: Optional[date] = None, db: Session = Depends(get_db),
                            current_user: models.User = Depends(get_current_user)):
    """Balance sheet: activity money plus entered assets, less entered liabilities."""
    return finance.balance_sheet(db, as_of)


# ---------------- Assets & liabilities (balance-sheet items) ----------------
@app.get("/api/assets", response_model=List[schemas.AssetItemOut])
def list_assets(include_inactive: bool = False, db: Session = Depends(get_db),
                current_user: models.User = Depends(get_current_user)):
    q = db.query(models.AssetItem)
    if not include_inactive:
        q = q.filter(models.AssetItem.is_active.is_(True))
    return q.order_by(models.AssetItem.kind, models.AssetItem.name).all()


@app.post("/api/assets", response_model=schemas.AssetItemOut, status_code=status.HTTP_201_CREATED)
def create_asset(data: schemas.AssetItemCreate, db: Session = Depends(get_db),
                 current_user: models.User = Depends(require_editor)):
    if data.kind not in finance.ASSET_KINDS:
        raise HTTPException(status_code=400,
                            detail=f"kind must be one of: {', '.join(finance.ASSET_KINDS)}")
    item = models.AssetItem(**data.model_dump(), created_by=current_user.id)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@app.put("/api/assets/{item_id}", response_model=schemas.AssetItemOut)
def update_asset(item_id: int, data: schemas.AssetItemUpdate, db: Session = Depends(get_db),
                 current_user: models.User = Depends(require_editor)):
    item = db.query(models.AssetItem).filter(models.AssetItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Asset not found")
    values = data.model_dump(exclude_unset=True)
    if values.get("kind") and values["kind"] not in finance.ASSET_KINDS:
        raise HTTPException(status_code=400,
                            detail=f"kind must be one of: {', '.join(finance.ASSET_KINDS)}")
    for field, value in values.items():
        setattr(item, field, value)
    db.commit()
    db.refresh(item)
    return item


@app.delete("/api/assets/{item_id}")
def delete_asset(item_id: int, db: Session = Depends(get_db),
                 current_user: models.User = Depends(require_editor)):
    item = db.query(models.AssetItem).filter(models.AssetItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Asset not found")
    db.delete(item)
    db.commit()
    return {"ok": True}


@app.get("/api/liabilities", response_model=List[schemas.LiabilityItemOut])
def list_liabilities(include_inactive: bool = False, db: Session = Depends(get_db),
                     current_user: models.User = Depends(get_current_user)):
    q = db.query(models.LiabilityItem)
    if not include_inactive:
        q = q.filter(models.LiabilityItem.is_active.is_(True))
    return q.order_by(models.LiabilityItem.kind, models.LiabilityItem.name).all()


@app.post("/api/liabilities", response_model=schemas.LiabilityItemOut, status_code=status.HTTP_201_CREATED)
def create_liability(data: schemas.LiabilityItemCreate, db: Session = Depends(get_db),
                     current_user: models.User = Depends(require_editor)):
    if data.kind not in finance.LIABILITY_KINDS:
        raise HTTPException(status_code=400,
                            detail=f"kind must be one of: {', '.join(finance.LIABILITY_KINDS)}")
    item = models.LiabilityItem(**data.model_dump(), created_by=current_user.id)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@app.put("/api/liabilities/{item_id}", response_model=schemas.LiabilityItemOut)
def update_liability(item_id: int, data: schemas.LiabilityItemUpdate, db: Session = Depends(get_db),
                     current_user: models.User = Depends(require_editor)):
    item = db.query(models.LiabilityItem).filter(models.LiabilityItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Liability not found")
    values = data.model_dump(exclude_unset=True)
    if values.get("kind") and values["kind"] not in finance.LIABILITY_KINDS:
        raise HTTPException(status_code=400,
                            detail=f"kind must be one of: {', '.join(finance.LIABILITY_KINDS)}")
    for field, value in values.items():
        setattr(item, field, value)
    db.commit()
    db.refresh(item)
    return item


@app.delete("/api/liabilities/{item_id}")
def delete_liability(item_id: int, db: Session = Depends(get_db),
                     current_user: models.User = Depends(require_editor)):
    item = db.query(models.LiabilityItem).filter(models.LiabilityItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Liability not found")
    db.delete(item)
    db.commit()
    return {"ok": True}


# ---------------- Backup download ----------------
def _backup_filename(ext: str) -> str:
    return f"family_finance_backup_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.{ext}"


@app.get("/api/export/csv")
def export_csv(db: Session = Depends(get_db), current_user: models.User = Depends(require_downloader)):
    transactions = crud.list_transactions(db, limit=100000)
    text = backup_io.build_csv_text(transactions)
    crud.log_backup(db, "export_csv", current_user, len(transactions))
    return StreamingResponse(
        iter([text]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{_backup_filename("csv")}"'},
    )


@app.get("/api/export/excel")
def export_excel(db: Session = Depends(get_db), current_user: models.User = Depends(require_downloader)):
    """Weekly backup: the workbook this produces is re-importable as-is."""
    transactions = crud.list_transactions(db, limit=100000)
    categories = crud.list_categories(db)
    try:
        content = backup_io.build_workbook_bytes(transactions, categories)
    except ImportError:
        raise HTTPException(status_code=500, detail="openpyxl not installed on server")
    crud.log_backup(db, "export_excel", current_user, len(transactions))
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{_backup_filename("xlsx")}"'},
    )


# ---------------- Backup status / restore ----------------
def _storage_is_persistent() -> bool:
    """False when the app writes to a local SQLite file (lost on redeploy/spin-down)."""
    return not DATABASE_URL.startswith("sqlite")


STORAGE_WARNING = (
    "Storage is NOT persistent: data is kept in a local SQLite file. On hosts such as "
    "Render's free tier the filesystem is erased on every redeploy and every spin-down, "
    "so accounts and transactions entered here will disappear. Set the DATABASE_URL "
    "environment variable to a persistent database (e.g. a free Neon or Supabase "
    "PostgreSQL) and the problem is solved."
)


@app.get("/api/admin/backup-status", response_model=schemas.BackupStatus)
def backup_status(db: Session = Depends(get_db), current_user: models.User = Depends(require_admin)):
    """Drives the weekly 'download your Excel backup' reminder."""
    db_ok, db_error = _database_check()
    status = crud.backup_status(db)
    persistent = _storage_is_persistent()
    status["persistent_storage"] = persistent
    status["storage_note"] = None if persistent else STORAGE_WARNING
    # Admin-only diagnostics: password-free, and kept out of the public /healthz.
    status["database_ok"] = db_ok
    status["database_error"] = db_error
    status["database_target"] = describe_url()
    status["app_version"] = app.version
    status["started_at"] = STARTED_AT
    return status


@app.post("/api/admin/import", response_model=schemas.ImportResult)
async def import_backup(
    file: UploadFile = File(...),
    confirm: str = Form(""),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin),
):
    """Replace ALL transactions with the contents of a backup file (xlsx/csv).

    The file is fully validated before anything is written, and the replace runs
    in a single transaction, so a bad or unsafe file cannot destroy data.
    """
    if confirm.strip().upper() != "REPLACE_ALL":
        raise HTTPException(
            status_code=400,
            detail="Safety confirmation missing. The request must send confirm=REPLACE_ALL.",
        )

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="The uploaded file is empty")
    if len(content) > 25 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File is larger than 25 MB")

    try:
        parsed = backup_io.parse_backup_file(file.filename or "backup.xlsx", content)
    except backup_io.BackupFormatError as exc:
        raise HTTPException(status_code=400,
                            detail={"message": exc.message, "errors": exc.errors[:50]})

    if not parsed.rows:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "No valid transaction rows found – nothing was changed.",
                "errors": parsed.errors[:50],
            },
        )

    bad_ratio = len(parsed.errors) / max(1, parsed.total_data_rows)
    if parsed.errors and bad_ratio > 0.05:
        raise HTTPException(
            status_code=400,
            detail={
                "message": (f"{len(parsed.errors)} of {parsed.total_data_rows} rows could not be "
                            "read (>5%). Refusing to replace your data – please fix the file."),
                "errors": parsed.errors[:50],
            },
        )

    try:
        result = crud.replace_all_transactions(db, parsed.rows, parsed.categories, current_user.id)
    except Exception as exc:  # noqa: BLE001 - surfaced to the admin
        raise HTTPException(status_code=500, detail=f"Import failed, nothing changed: {exc}")

    crud.log_backup(db, "import", current_user, result["imported"],
                    note=f"Replaced {result['deleted']} rows from '{file.filename}'")
    # the restored data needs its month-end sweep rebuilt from scratch
    _refresh_offsets(db)

    warnings = []
    if parsed.errors:
        warnings.append(f"{len(parsed.errors)} row(s) were skipped because they could not be read.")
    if any(r.category == backup_io.UNCATEGORIZED for r in parsed.rows):
        warnings.append("Some rows had no category and were filed under 'Uncategorized'.")

    return schemas.ImportResult(
        ok=True,
        sheet=parsed.sheet_name,
        imported=result["imported"],
        deleted=result["deleted"],
        categories_created=result["categories_created"],
        categories_updated=result["categories_updated"],
        rows_read=parsed.total_data_rows,
        skipped_rows=parsed.errors[:50],
        warnings=warnings,
    )

