import io
import csv
from datetime import date, datetime, timedelta
from typing import List, Optional

from fastapi import FastAPI, Depends, HTTPException, status, Query, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from . import models, schemas, crud, auth
from .database import engine, get_db, SessionLocal
from .auth import (
    authenticate_user, create_access_token, get_current_user,
    require_roles, require_master, require_admin, require_editor, require_downloader,
    ROLE_MASTER, ROLE_ADMIN, ROLE_EDITOR, ROLE_VIEWER, ROLE_DOWNLOADER, ALL_ROLES,
)

app = FastAPI(title="Family Financial Control System", version="1.0.0")

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
    models.Base.metadata.create_all(bind=engine)
    from .seed import seed_initial_data
    seed_initial_data()


# ---------------- Frontend ----------------
@app.get("/")
def root():
    return FileResponse("static/index.html")


# ---------------- Auth ----------------
@app.post("/api/auth/register", response_model=schemas.UserOut)
def register(user: schemas.UserCreate, db: Session = Depends(get_db)):
    if crud.get_user_by_username(db, user.username):
        raise HTTPException(status_code=400, detail="Username already registered")
    if crud.get_user_by_email(db, user.email):
        raise HTTPException(status_code=400, detail="Email already registered")
    db_user = crud.create_user(db, user, role=ROLE_VIEWER, is_active=False, is_approved=False)
    return db_user


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
@app.get("/api/users", response_model=List[schemas.UserOut])
def list_users(db: Session = Depends(get_db), current_user: models.User = Depends(require_admin)):
    return crud.list_users(db)


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


# ---------------- Export ----------------
@app.get("/api/export/csv")
def export_csv(db: Session = Depends(get_db), current_user: models.User = Depends(require_downloader)):
    transactions = crud.list_transactions(db, limit=100000)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", "Type", "Category", "Amount", "Description"])
    for t in transactions:
        writer.writerow([t.date, t.type, t.category.name if t.category else "", t.amount, t.description or ""])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=transactions.csv"},
    )


@app.get("/api/export/excel")
def export_excel(db: Session = Depends(get_db), current_user: models.User = Depends(require_downloader)):
    try:
        import openpyxl
        from openpyxl import Workbook
    except ImportError:
        raise HTTPException(status_code=500, detail="openpyxl not installed on server")
    transactions = crud.list_transactions(db, limit=100000)
    wb = Workbook()
    ws = wb.active
    ws.title = "Transactions"
    ws.append(["Date", "Type", "Category", "Amount", "Description"])
    for t in transactions:
        ws.append([str(t.date), t.type, t.category.name if t.category else "", t.amount, t.description or ""])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=transactions.xlsx"},
    )
