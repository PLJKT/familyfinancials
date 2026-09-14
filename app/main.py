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

from . import models, schemas, crud, auth, backup_io
from .database import engine, get_db, SessionLocal, DATABASE_URL, safe_url, describe_url, BACKEND
from .auth import (
    authenticate_user, create_access_token, get_current_user,
    require_roles, require_master, require_admin, require_editor, require_downloader,
    ROLE_MASTER, ROLE_ADMIN, ROLE_EDITOR, ROLE_VIEWER, ROLE_DOWNLOADER, ALL_ROLES,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("familyfinancials")

app = FastAPI(title="Family Financial Control System", version="1.1.0")


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
    logger.info("Starting Family Financial Control System | storage=%s | %s | persistent=%s",
                BACKEND, describe_url(), not DATABASE_URL.startswith("sqlite"))
    try:
        models.Base.metadata.create_all(bind=engine)
        from .seed import seed_initial_data
        seed_initial_data()
    except Exception:
        logger.exception("STARTUP FAILED while preparing the database at %s", safe_url())
        raise
    logger.info("Startup complete - the app is ready to serve requests.")


@app.get("/healthz")
def healthz():
    """Liveness + database check; handy when a deploy fails (open /healthz)."""
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_ok, db_error = True, None
    except Exception as exc:
        db_ok, db_error = False, f"{type(exc).__name__}: {exc}"
    return {
        "status": "ok" if db_ok else "degraded",
        "database_ok": db_ok,
        "backend": BACKEND,
        "database_url": safe_url(),
        "database_target": describe_url(),
        "storage_persistent": not DATABASE_URL.startswith("sqlite"),
        "error": db_error,
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
    return crud.create_transaction(db, data, current_user.id)


@app.put("/api/transactions/{trx_id}", response_model=schemas.TransactionOut)
def edit_transaction(trx_id: int, data: schemas.TransactionUpdate, db: Session = Depends(get_db),
                     current_user: models.User = Depends(require_editor)):
    trx = db.query(models.Transaction).filter(models.Transaction.id == trx_id).first()
    if not trx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return crud.update_transaction(db, trx, data)


@app.delete("/api/transactions/{trx_id}")
def remove_transaction(trx_id: int, db: Session = Depends(get_db),
                       current_user: models.User = Depends(require_editor)):
    trx = db.query(models.Transaction).filter(models.Transaction.id == trx_id).first()
    if not trx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    crud.delete_transaction(db, trx)
    return {"ok": True}


# ---------------- Reports ----------------
@app.post("/api/reports/summary", response_model=schemas.SummaryResponse)
def report_summary(query: schemas.ReportQuery, db: Session = Depends(get_db),
                   current_user: models.User = Depends(get_current_user)):
    return crud.build_summary(db, query)


@app.get("/api/dashboard")
def dashboard(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    return crud.dashboard_kpis(db)


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
    status = crud.backup_status(db)
    persistent = _storage_is_persistent()
    status["persistent_storage"] = persistent
    status["storage_note"] = None if persistent else STORAGE_WARNING
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

