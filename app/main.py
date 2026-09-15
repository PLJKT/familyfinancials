import io
import csv
import logging
from datetime import date, datetime, timedelta
from typing import List, Optional

from fastapi import FastAPI, Depends, HTTPException, status, Query, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
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


app = FastAPI(title="Family Financial Control System", version="1.3.0")


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
            sweep = finance.apply_month_end_offsets(db)
        logger.info("Month-end saving offsets: %s created, %s updated, %s removed (%s closed months)",
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
        return finance.apply_month_end_offsets(db)
    except Exception:
        logger.exception("Month-end saving offsets could not be refreshed")
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
    """Record a saving contribution (stored as a Savings transaction)."""
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
        date=data.date, type="Savings", category_id=category_id, amount=float(data.amount),
        description=data.note or "Saving contribution",
        member_id=data.member_id or current_user.id,
    )
    trx = crud.create_transaction(db, payload, current_user.id)
    _refresh_offsets(db)
    db.refresh(trx)
    return trx


@app.post("/api/admin/offsets/run", response_model=schemas.OffsetRunResult)
def run_offsets(db: Session = Depends(get_db),
                current_user: models.User = Depends(require_admin)):
    """Apply / refresh the automatic month-end saving offsets (idempotent)."""
    return _refresh_offsets(db)


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

