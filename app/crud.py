from datetime import date
from typing import List, Optional
from sqlalchemy import func, case, cast, String
from sqlalchemy.orm import Session, joinedload

from . import models, schemas


# ---------- Users ----------
def get_user_by_username(db: Session, username: str) -> Optional[models.User]:
    return db.query(models.User).filter(models.User.username == username).first()


def get_user_by_email(db: Session, email: str) -> Optional[models.User]:
    return db.query(models.User).filter(models.User.email == email).first()


def list_users(db: Session) -> List[models.User]:
    return db.query(models.User).order_by(models.User.created_at.desc()).all()


def create_user(db: Session, data: schemas.UserCreate, role: str = "viewer",
                is_active: bool = False, is_approved: bool = False) -> models.User:
    from .auth import get_password_hash
    user = models.User(
        username=data.username,
        email=data.email,
        full_name=data.full_name,
        hashed_password=get_password_hash(data.password),
        role=role,
        is_active=is_active,
        is_approved=is_approved,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def update_user(db: Session, user: models.User, data: schemas.UserUpdate) -> models.User:
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(user, field, value)
    db.commit()
    db.refresh(user)
    return user


# ---------- Categories ----------
def list_categories(db: Session) -> List[models.Category]:
    return db.query(models.Category).order_by(models.Category.group, models.Category.name).all()


def get_category(db: Session, category_id: int) -> Optional[models.Category]:
    return db.query(models.Category).filter(models.Category.id == category_id).first()


def create_category(db: Session, data: schemas.CategoryCreate) -> models.Category:
    cat = models.Category(**data.model_dump())
    db.add(cat)
    db.commit()
    db.refresh(cat)
    return cat


def update_category(db: Session, category: models.Category, data: schemas.CategoryBase) -> models.Category:
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(category, field, value)
    db.commit()
    db.refresh(category)
    return category


def delete_category(db: Session, category: models.Category) -> None:
    db.delete(category)
    db.commit()


# ---------- Transactions ----------
def list_transactions(db: Session, start_date: Optional[date] = None, end_date: Optional[date] = None,
                      category_ids: Optional[List[int]] = None, types: Optional[List[str]] = None,
                      limit: int = 1000, offset: int = 0) -> List[models.Transaction]:
    q = db.query(models.Transaction).options(joinedload(models.Transaction.category))
    if start_date:
        q = q.filter(models.Transaction.date >= start_date)
    if end_date:
        q = q.filter(models.Transaction.date <= end_date)
    if category_ids:
        q = q.filter(models.Transaction.category_id.in_(category_ids))
    if types:
        q = q.filter(models.Transaction.type.in_(types))
    q = q.order_by(models.Transaction.date.desc(), models.Transaction.id.desc())
    return q.offset(offset).limit(limit).all()


def create_transaction(db: Session, data: schemas.TransactionCreate, user_id: Optional[int] = None) -> models.Transaction:
    trx = models.Transaction(
        date=data.date,
        type=data.type,
        category_id=data.category_id,
        amount=data.amount,
        description=data.description,
        created_by=user_id,
    )
    db.add(trx)
    db.commit()
    db.refresh(trx)
    return trx


def update_transaction(db: Session, trx: models.Transaction, data: schemas.TransactionUpdate) -> models.Transaction:
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(trx, field, value)
    db.commit()
    db.refresh(trx)
    return trx


def delete_transaction(db: Session, trx: models.Transaction) -> None:
    db.delete(trx)
    db.commit()


# ---------- Reports ----------
def build_summary(db: Session, query: schemas.ReportQuery) -> schemas.SummaryResponse:
    """Aggregate transactions by month/year/category with income/expense/savings split."""
    q = db.query(models.Transaction)
    if query.start_date:
        q = q.filter(models.Transaction.date >= query.start_date)
    if query.end_date:
        q = q.filter(models.Transaction.date <= query.end_date)
    if query.category_ids:
        q = q.filter(models.Transaction.category_id.in_(query.category_ids))
    if query.types:
        q = q.filter(models.Transaction.type.in_(query.types))

    if query.group_by == "year":
        key_expr = func.substr(cast(models.Transaction.date, String), 1, 4)
    elif query.group_by == "category":
        key_expr = models.Category.name
        q = q.join(models.Category, models.Transaction.category_id == models.Category.id)
    else:  # month
        key_expr = func.substr(cast(models.Transaction.date, String), 1, 7)

    income_col = func.coalesce(func.sum(case((models.Transaction.type == "Income", models.Transaction.amount), else_=0.0)), 0.0)
    expense_col = func.coalesce(func.sum(case((models.Transaction.type == "Expenses", models.Transaction.amount), else_=0.0)), 0.0)
    savings_col = func.coalesce(func.sum(case((models.Transaction.type == "Savings", models.Transaction.amount), else_=0.0)), 0.0)

    rows = (
        q.with_entities(key_expr.label("key"), income_col.label("income"),
                        expense_col.label("expenses"), savings_col.label("savings"))
        .group_by("key")
        .order_by("key")
        .all()
    )

    result_rows = []
    tot_inc = tot_exp = tot_sav = 0.0
    for r in rows:
        net = (r.income or 0.0) - (r.expenses or 0.0)
        result_rows.append(schemas.SummaryRow(
            key=str(r.key), income=float(r.income or 0.0), expenses=float(r.expenses or 0.0),
            savings=float(r.savings or 0.0), net=net,
        ))
        tot_inc += float(r.income or 0.0)
        tot_exp += float(r.expenses or 0.0)
        tot_sav += float(r.savings or 0.0)

    totals = schemas.SummaryRow(key="TOTAL", income=tot_inc, expenses=tot_exp,
                                savings=tot_sav, net=tot_inc - tot_exp)
    return schemas.SummaryResponse(rows=result_rows, totals=totals)


def dashboard_kpis(db: Session) -> dict:
    """Return high-level KPIs used by the dashboard."""
    total_income = db.query(func.coalesce(func.sum(models.Transaction.amount), 0.0)).filter(
        models.Transaction.type == "Income").scalar() or 0.0
    total_expenses = db.query(func.coalesce(func.sum(models.Transaction.amount), 0.0)).filter(
        models.Transaction.type == "Expenses").scalar() or 0.0
    total_savings = db.query(func.coalesce(func.sum(models.Transaction.amount), 0.0)).filter(
        models.Transaction.type == "Savings").scalar() or 0.0
    tx_count = db.query(func.count(models.Transaction.id)).scalar() or 0
    balance = float(total_income) - float(total_expenses)

    # last 12 months trend
    twelve_months_ago = date.today().replace(year=date.today().year - 1)
    trend_q = (
        db.query(func.substr(cast(models.Transaction.date, String), 1, 7).label("month"),
                 func.coalesce(func.sum(case((models.Transaction.type == "Income", models.Transaction.amount), else_=0.0)), 0.0).label("income"),
                 func.coalesce(func.sum(case((models.Transaction.type == "Expenses", models.Transaction.amount), else_=0.0)), 0.0).label("expenses"))
        .filter(models.Transaction.date >= twelve_months_ago)
        .group_by("month")
        .order_by("month")
        .all()
    )
    trend = [{"month": t.month, "income": float(t.income), "expenses": float(t.expenses)} for t in trend_q]

    return {
        "total_income": float(total_income),
        "total_expenses": float(total_expenses),
        "total_savings": float(total_savings),
        "balance": balance,
        "transaction_count": int(tx_count),
        "trend": trend,
    }
