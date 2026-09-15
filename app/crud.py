import os
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple
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
        member_id=getattr(data, "member_id", None) or user_id,
        account_id=getattr(data, "account_id", None),
        direction=getattr(data, "direction", None),
        lender=getattr(data, "lender", None),
        funded_by=getattr(data, "funded_by", None),
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
    # savings are reported net of withdrawals; loans never touch this statement
    savings_col = (func.coalesce(func.sum(case((models.Transaction.type == "Savings", models.Transaction.amount), else_=0.0)), 0.0)
                   - func.coalesce(func.sum(case((models.Transaction.type == "Withdrawal", models.Transaction.amount), else_=0.0)), 0.0))

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
    # savings and cash come from the accounts, so withdrawals are netted off
    from . import finance as _finance
    balances = _finance.account_balances(db)
    total_savings = balances["savings"]
    cash_balance = balances["cash"]

    # what moved into savings during the current month (flow, not stock)
    this_month = _finance.month_of(date.today())
    month_entry = _finance.month_totals(db).get(this_month, {})
    savings_in_month = month_entry.get("savings_net", 0.0)
    tx_count = db.query(func.count(models.Transaction.id)).scalar() or 0

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
        "total_savings": float(total_savings),          # savings balance (stock)
        "cash_balance": float(cash_balance),
        "savings_this_month": float(savings_in_month),  # movement in the running month
        "transaction_count": int(tx_count),
        "trend": trend,
    }


# ---------- Backup log / weekly reminder ----------
def log_backup(db: Session, kind: str, user: Optional[models.User] = None,
               row_count: Optional[int] = None, note: Optional[str] = None) -> models.BackupLog:
    entry = models.BackupLog(
        kind=kind,
        user_id=getattr(user, "id", None),
        username=getattr(user, "username", None),
        row_count=row_count,
        note=note,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def list_backup_logs(db: Session, limit: int = 10) -> List[models.BackupLog]:
    return (db.query(models.BackupLog)
            .order_by(models.BackupLog.created_at.desc(), models.BackupLog.id.desc())
            .limit(limit).all())


def backup_interval_days() -> int:
    try:
        return max(1, int(os.getenv("BACKUP_REMINDER_DAYS", "7")))
    except ValueError:
        return 7


def backup_status(db: Session) -> dict:
    """State for the admin 'download a backup weekly' reminder."""
    interval = backup_interval_days()
    last_export = (db.query(models.BackupLog)
                   .filter(models.BackupLog.kind.in_(["export_excel", "export_csv"]))
                   .order_by(models.BackupLog.created_at.desc(), models.BackupLog.id.desc())
                   .first())
    last_import = (db.query(models.BackupLog)
                   .filter(models.BackupLog.kind == "import")
                   .order_by(models.BackupLog.created_at.desc(), models.BackupLog.id.desc())
                   .first())

    days_since = None
    if last_export is not None:
        days_since = (datetime.utcnow() - last_export.created_at).total_seconds() / 86400.0

    count = db.query(func.count(models.Transaction.id)).scalar() or 0
    return {
        "interval_days": interval,
        "due": last_export is None or days_since >= interval,
        "days_since_last_backup": round(days_since, 2) if days_since is not None else None,
        "last_backup_at": last_export.created_at if last_export else None,
        "last_backup_kind": last_export.kind if last_export else None,
        "last_import_at": last_import.created_at if last_import else None,
        "transaction_count": int(count),
        "history": list_backup_logs(db, 10),
    }


# ---------- Restore (replace everything) ----------
def get_or_create_category(db: Session, name: str, type_hint: Optional[str] = None,
                           group: Optional[str] = None, description: Optional[str] = None,
                           cache: Optional[Dict[str, models.Category]] = None,
                           ) -> Tuple[models.Category, bool, bool]:
    """Find a category by (case-insensitive) name, creating/updating as needed.

    Returns (category, created, updated).
    """
    key = (name or "").strip().casefold()
    cache = cache if cache is not None else {}

    cat = cache.get(key)
    if cat is None:
        cat = db.query(models.Category).filter(func.lower(models.Category.name) == key).first()
        if cat is None:
            cat = models.Category(
                name=(name or "").strip() or "Uncategorized",
                type=type_hint or "Expenses",
                group=group or None,
                description=description or None,
            )
            db.add(cat)
            db.flush()
            cache[key] = cat
            return cat, True, False

    updated = False
    if type_hint and cat.type != type_hint:
        cat.type = type_hint
        updated = True
    if group and cat.group != group:
        cat.group = group
        updated = True
    if description and cat.description != description:
        cat.description = description
        updated = True
    cache[key] = cat
    return cat, False, updated


def replace_all_transactions(db: Session, rows, categories_meta=None,
                             user_id: Optional[int] = None) -> dict:
    """Atomically replace every transaction with the rows of a backup file.

    Categories are matched by name (created if missing, never deleted) so that
    category groups survive an Excel round-trip.
    """
    created = updated = 0
    cache: Dict[str, models.Category] = {}

    # username / full name -> user id, so family-member attribution survives a restore
    members: Dict[str, int] = {}
    for u in db.query(models.User).all():
        if u.username:
            members[u.username.strip().casefold()] = u.id
        if u.full_name:
            members.setdefault(u.full_name.strip().casefold(), u.id)

    def member_id_for(row) -> Optional[int]:
        label = getattr(row, "member", None)
        if not label:
            return None
        return members.get(str(label).strip().casefold())

    def category_type_for(row_type: str) -> str:
        """Which family a category belongs to — keeps 'Saving' from being re-typed."""
        return {"Withdrawal": "Savings", "Transfer": "Savings", "Loan": "Loan"}.get(row_type, row_type)

    def upsert(name, type_hint=None, group=None, description=None):
        nonlocal created, updated
        cat, was_created, was_updated = get_or_create_category(
            db, name, type_hint, group, description, cache)
        created += 1 if was_created else 0
        updated += 1 if was_updated else 0
        return cat

    try:
        # 1. categories from the metadata sheet (if the file has one)
        for meta in categories_meta or []:
            upsert(meta.get("name"), meta.get("type"), meta.get("group"), meta.get("description"))

        # 2. categories referenced by the transaction rows
        for row in rows:
            upsert(row.category, category_type_for(row.type))
        db.flush()

        # 3. wipe and re-insert in one transaction
        deleted = int(db.query(func.count(models.Transaction.id)).scalar() or 0)
        db.query(models.Transaction).delete(synchronize_session=False)
        db.flush()

        new_rows = []
        for row in rows:
            cat = cache.get(row.category.strip().casefold())
            if cat is None:  # defensive: should never happen
                cat = upsert(row.category, category_type_for(row.type))
            row_type = row.type
            direction = getattr(row, "direction", None)
            if row_type == "Loan":
                direction = "repay" if (direction or "").startswith("rep") else "borrow"
            else:
                direction = None
            new_rows.append(models.Transaction(
                date=row.date,
                type=row_type,
                category_id=cat.id,
                amount=row.amount,
                description=row.description or None,
                created_by=user_id,
                member_id=member_id_for(row),
                auto_offset_month=getattr(row, "auto_offset_month", None),
                direction=direction,
                lender=getattr(row, "lender", None) if row_type == "Loan" else None,
                funded_by=getattr(row, "funded_by", None) if row_type in ("Savings", "Transfer") else None,
            ))
        db.add_all(new_rows)
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {
        "deleted": deleted,
        "imported": len(new_rows),
        "categories_created": created,
        "categories_updated": updated,
    }

