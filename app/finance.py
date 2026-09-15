"""Financial engines: automatic month-end saving offsets, savings bookkeeping,
the income statement and the balance sheet.

Model in one line: the difference between a month's income and its expenses is
swept into savings when the month closes, so a finished month's savings always
equal its surplus. Manual saving entries are kept, and the sweep is posted as a
separate, clearly labelled, idempotent adjustment row.
"""
from calendar import monthrange
from datetime import date
from typing import Optional

from sqlalchemy import String, cast, func
from sqlalchemy.orm import Session

from . import models

MONTH_FMT = "%Y-%m"
SAVING_CATEGORY_NAME = "Saving"
ASSET_KINDS = ["property", "vehicle", "investment", "cash", "other"]
LIABILITY_KINDS = ["mortgage", "car_loan", "personal_loan", "credit_card", "other"]


def month_of(d: date) -> str:
    return d.strftime(MONTH_FMT)


def month_bounds(month: str):
    """'2026-08' -> (first day, last day)."""
    year, mon = (int(p) for p in month.split("-"))
    return date(year, mon, 1), date(year, mon, monthrange(year, mon)[1])


def _month_expr():
    """Portable YYYY-MM of a transaction date (SQLite and PostgreSQL)."""
    return func.substr(cast(models.Transaction.date, String), 1, 7)


def _member_expr():
    """Whose saving it is; rows saved before member tracking fall back to the author."""
    return func.coalesce(models.Transaction.member_id, models.Transaction.created_by)


# --------------------------------------------------------------------- totals
def month_totals(db: Session) -> dict:
    """Per-month income / expenses / savings, splitting manual savings from offsets."""
    buckets: dict = {}

    def bucket(month: str) -> dict:
        return buckets.setdefault(month, {
            "month": month, "income": 0.0, "expenses": 0.0,
            "savings_manual": 0.0, "savings_offset": 0.0,
            "entries": 0, "manual_entries": 0,
        })

    for month, ttype, total, n in (
        db.query(_month_expr(), models.Transaction.type,
                 func.sum(models.Transaction.amount), func.count(models.Transaction.id))
        .filter(models.Transaction.type != "Savings")
        .group_by(_month_expr(), models.Transaction.type)
        .all()
    ):
        e = bucket(month)
        e["entries"] += int(n or 0)
        e["manual_entries"] += int(n or 0)
        if ttype == "Income":
            e["income"] += float(total or 0.0)
        else:
            e["expenses"] += float(total or 0.0)

    for month, is_manual, total, n in (
        db.query(_month_expr(), models.Transaction.auto_offset_month.is_(None),
                 func.sum(models.Transaction.amount), func.count(models.Transaction.id))
        .filter(models.Transaction.type == "Savings")
        .group_by(_month_expr(), models.Transaction.auto_offset_month.is_(None))
        .all()
    ):
        e = bucket(month)
        e["entries"] += int(n or 0)
        if is_manual:
            e["savings_manual"] += float(total or 0.0)
            e["manual_entries"] += int(n or 0)
        else:
            e["savings_offset"] += float(total or 0.0)

    for e in buckets.values():
        e["surplus"] = e["income"] - e["expenses"]
        e["savings_total"] = e["savings_manual"] + e["savings_offset"]
        e["offset_target"] = e["surplus"] - e["savings_manual"]
        e["unallocated"] = e["surplus"] - e["savings_total"]
    return buckets


def _saving_category(db: Session) -> models.Category:
    cat = db.query(models.Category).filter(models.Category.name == SAVING_CATEGORY_NAME).first()
    if cat is None:
        cat = db.query(models.Category).filter(models.Category.type == "Savings").first()
    if cat is None:
        cat = models.Category(name=SAVING_CATEGORY_NAME, type="Savings", group="储蓄")
        db.add(cat)
        db.flush()
    return cat


def apply_month_end_offsets(db: Session, today: Optional[date] = None) -> dict:
    """Give every closed month exactly one automatic saving offset.

    ``offset = (income - expenses) - savings already recorded that month``
    so that the month's savings end up equal to its surplus. Idempotent: the row
    is matched by ``auto_offset_month`` and updated in place, never duplicated,
    which makes it safe to run on every startup and on demand.
    """
    today = today or date.today()
    current = month_of(today)
    totals = month_totals(db)
    created = updated = removed = 0
    changes = []
    category = None

    # one query for every existing offset row keeps this cheap enough to run
    # after each transaction write, so the sweep is always up to date
    existing_rows = {
        t.auto_offset_month: t
        for t in db.query(models.Transaction)
        .filter(models.Transaction.auto_offset_month.isnot(None))
        .all()
    }

    for month in sorted(totals):
        if month >= current:
            continue                                    # still running, not closed
        entry = totals[month]
        existing = existing_rows.get(month)
        if entry["manual_entries"] == 0:
            if existing is not None:                    # month lost its real data
                db.delete(existing)
                removed += 1
            continue

        target = round(entry["offset_target"], 2)
        if existing is not None:
            if abs(float(existing.amount) - target) > 0.005:
                changes.append({"month": month, "action": "updated",
                                "from": float(existing.amount), "to": target})
                existing.amount = target
                updated += 1
        elif abs(target) > 0.005:
            if category is None:
                category = _saving_category(db)
            _, last = month_bounds(month)
            db.add(models.Transaction(
                date=last, type="Savings", category_id=category.id, amount=target,
                description=f"Automatic month-end saving offset for {month}",
                auto_offset_month=month, member_id=None, created_by=None,
            ))
            changes.append({"month": month, "action": "created", "from": None, "to": target})
            created += 1

    if created or updated or removed:
        db.commit()
    return {
        "created": created, "updated": updated, "removed": removed,
        "closed_months": [m for m in sorted(totals) if m < current],
        "changes": changes,
    }


# -------------------------------------------------------------------- savings
def savings_summary(db: Session, months: int = 12, today: Optional[date] = None) -> dict:
    """Month-by-month savings, split per family member, with the offset status."""
    today = today or date.today()
    totals = month_totals(db)
    current = month_of(today)

    per_member: dict = {}
    for month, member_id, total in (
        db.query(_month_expr(), _member_expr(), func.sum(models.Transaction.amount))
        .filter(models.Transaction.type == "Savings",
                models.Transaction.auto_offset_month.is_(None))
        .group_by(_month_expr(), _member_expr())
        .all()
    ):
        per_member.setdefault(month, {})[str(int(member_id) if member_id else 0)] = float(total or 0.0)

    members = [
        {"id": u.id, "name": u.full_name or u.username}
        for u in db.query(models.User).order_by(models.User.id).all()
    ]

    selected = sorted(totals)[-months:] if months else sorted(totals)
    out = []
    for m in selected:
        e = totals[m]
        out.append({
            "month": m, "closed": m < current,
            "income": e["income"], "expenses": e["expenses"], "surplus": e["surplus"],
            "savings_manual": e["savings_manual"], "savings_offset": e["savings_offset"],
            "savings_total": e["savings_total"], "unallocated": e["unallocated"],
            "offset_target": e["offset_target"],
            "offset_applied": abs(e["savings_offset"]) > 0.005,
            "members": per_member.get(m, {}),
        })

    keys = ("income", "expenses", "surplus", "savings_manual", "savings_offset",
            "savings_total", "unallocated")
    grand = {k: sum(e[k] for e in out) for k in keys}
    return {"months": out, "members": members, "totals": grand,
            "current_month": current, "auto_offset": True}


# ----------------------------------------------------------------- statements
def _in_range(query, start: Optional[date], end: Optional[date]):
    if start:
        query = query.filter(models.Transaction.date >= start)
    if end:
        query = query.filter(models.Transaction.date <= end)
    return query


def income_statement(db: Session, start: Optional[date] = None,
                     end: Optional[date] = None) -> dict:
    """Income / expenses / savings for a period, as a family income statement."""
    rows = _in_range(
        db.query(models.Category.type, models.Category.group, models.Category.name,
                 models.Transaction.auto_offset_month.is_(None),
                 func.sum(models.Transaction.amount))
        .join(models.Transaction, models.Transaction.category_id == models.Category.id),
        start, end,
    ).group_by(models.Category.type, models.Category.group, models.Category.name,
               models.Transaction.auto_offset_month.is_(None)).all()

    income_lines, expense_lines = [], []
    expense_groups: dict = {}
    savings_manual = savings_offset = 0.0
    for ctype, group, name, is_manual, total in rows:
        amount = float(total or 0.0)
        if ctype == "Income":
            income_lines.append({"category": name, "group": group, "amount": amount})
        elif ctype == "Expenses":
            expense_lines.append({"category": name, "group": group, "amount": amount})
            expense_groups[group or "—"] = expense_groups.get(group or "—", 0.0) + amount
        elif is_manual:
            savings_manual += amount
        else:
            savings_offset += amount

    income_lines.sort(key=lambda r: -r["amount"])
    expense_lines.sort(key=lambda r: -r["amount"])
    income_total = sum(r["amount"] for r in income_lines)
    expense_total = sum(r["amount"] for r in expense_lines)
    savings_total = savings_manual + savings_offset

    months = []
    first_month = month_of(start) if start else None
    last_month = month_of(end) if end else None
    for m, e in sorted(month_totals(db).items()):
        if first_month and m < first_month:
            continue
        if last_month and m > last_month:
            continue
        months.append({
            "month": m, "income": e["income"], "expenses": e["expenses"],
            "surplus": e["surplus"], "savings": e["savings_total"],
            "net": e["surplus"] - e["savings_total"],
        })

    return {
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "income_lines": income_lines, "income_total": income_total,
        "expense_lines": expense_lines, "expense_total": expense_total,
        "expense_groups": sorted(
            ({"group": g, "amount": a} for g, a in expense_groups.items()),
            key=lambda r: -r["amount"]),
        "surplus": income_total - expense_total,
        "savings_manual": savings_manual,
        "savings_offset": savings_offset,
        "savings_total": savings_total,
        "unallocated": (income_total - expense_total) - savings_total,
        "months": months,
    }


def balance_sheet(db: Session, as_of: Optional[date] = None) -> dict:
    """Balance sheet: money from activity plus entered assets, less entered debts."""

    def total_for(ttype: str) -> float:
        q = db.query(func.coalesce(func.sum(models.Transaction.amount), 0.0)).filter(
            models.Transaction.type == ttype)
        if as_of:
            q = q.filter(models.Transaction.date <= as_of)
        return float(q.scalar() or 0.0)

    income = total_for("Income")
    expenses = total_for("Expenses")
    savings = total_for("Savings")
    unallocated_cash = income - expenses - savings

    asset_items = (db.query(models.AssetItem).filter(models.AssetItem.is_active.is_(True))
                   .order_by(models.AssetItem.kind, models.AssetItem.name).all())
    liability_items = (db.query(models.LiabilityItem).filter(models.LiabilityItem.is_active.is_(True))
                       .order_by(models.LiabilityItem.kind, models.LiabilityItem.name).all())

    asset_rows = [{"id": a.id, "name": a.name, "kind": a.kind, "value": float(a.value or 0.0),
                   "acquired_on": a.acquired_on.isoformat() if a.acquired_on else None,
                   "note": a.note} for a in asset_items]
    liability_rows = [{"id": l.id, "name": l.name, "kind": l.kind,
                       "outstanding": float(l.outstanding or 0.0),
                       "monthly_payment": float(l.monthly_payment) if l.monthly_payment is not None else None,
                       "interest_rate": float(l.interest_rate) if l.interest_rate is not None else None,
                       "started_on": l.started_on.isoformat() if l.started_on else None,
                       "note": l.note} for l in liability_items]

    activity_assets = savings + unallocated_cash
    items_total = sum(r["value"] for r in asset_rows)
    liabilities_total = sum(r["outstanding"] for r in liability_rows)
    total_assets = activity_assets + items_total

    by_kind: dict = {}
    for r in asset_rows:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0.0) + r["value"]

    return {
        "as_of": as_of.isoformat() if as_of else None,
        "cash_and_savings": {
            "savings_accumulated": savings,
            "unallocated_cash": unallocated_cash,
            "total": activity_assets,
        },
        "asset_items": asset_rows,
        "asset_items_total": items_total,
        "asset_items_by_kind": by_kind,
        "total_assets": total_assets,
        "liability_items": liability_rows,
        "liabilities_total": liabilities_total,
        "net_worth": total_assets - liabilities_total,
        "from_activity": {"income": income, "expenses": expenses, "savings": savings},
        "kinds": {"asset": ASSET_KINDS, "liability": LIABILITY_KINDS},
    }
