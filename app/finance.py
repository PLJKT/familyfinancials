"""Financial engines: accounts and their balances, the month-end sweep,
the savings book, the income statement and the balance sheet.

The model, in one paragraph. Money lives in **accounts** — a cash account and a
savings account — that each start from an *opening balance* (a stock, never an
income or an expense). Income and expenses change cash, and therefore net worth.
Transfers move money between the family's own accounts and change nothing but the
split: `Savings` pays into savings, `Withdrawal` takes money back out. `Loan`
records borrowing (`borrow`: cash up, debt up) and repayment (`repay`: cash down,
debt down). Only income, expenses and loan interest move net worth, so

    net worth = opening balances + (income − expenses)

holds at every point in time — which is exactly what the balance sheet reports and
what the reconciliation view verifies month by month.
"""
from calendar import monthrange
from datetime import date
from typing import Optional

from sqlalchemy import String, cast, func, or_
from sqlalchemy.orm import Session

from . import models

MONTH_FMT = "%Y-%m"
SAVING_CATEGORY_NAME = "Saving"
ASSET_KINDS = ["property", "vehicle", "investment", "cash", "other"]
LIABILITY_KINDS = ["mortgage", "car_loan", "personal_loan", "credit_card", "other"]
DEFAULT_ACCOUNTS = [("Cash", "cash"), ("Savings", "savings")]
# a transfer funded by these did not come out of income, so the sweep ignores it
NON_INCOME_FUNDING = ("loan", "opening")


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
    """Whose saving it is; rows entered before member tracking fall back to the author."""
    return func.coalesce(models.Transaction.member_id, models.Transaction.created_by)


def _in_range(query, start: Optional[date], end: Optional[date]):
    if start:
        query = query.filter(models.Transaction.date >= start)
    if end:
        query = query.filter(models.Transaction.date <= end)
    return query


# ------------------------------------------------------------------- accounts
def ensure_default_accounts(db: Session) -> list:
    """Make sure a cash and a savings account exist, so every flow has a home."""
    created = []
    for name, kind in DEFAULT_ACCOUNTS:
        # any account of this kind (active or not) means the kind already has a home;
        # reactivating later is the operator's choice, never recreate a unique name
        exists = (db.query(models.Account)
                  .filter(models.Account.kind == kind).first())
        if exists is None:
            db.add(models.Account(name=name, kind=kind, opening_balance=0.0))
            created.append(name)
    if created:
        db.commit()
    return created


def list_accounts(db: Session, include_inactive: bool = False) -> list:
    query = db.query(models.Account)
    if not include_inactive:
        query = query.filter(models.Account.is_active.is_(True))
    return query.order_by(models.Account.kind.desc(), models.Account.name).all()


def account_balances(db: Session, as_of: Optional[date] = None, include_inactive: bool = False) -> dict:
    """Every account's balance, together with the movements that produced it."""
    accounts = list_accounts(db, include_inactive)
    kind_of = {a.id: a.kind for a in accounts}
    balances = {a.id: float(a.opening_balance or 0.0) for a in accounts}
    moves = {a.id: {"in": 0.0, "out": 0.0} for a in accounts}
    default = {}
    for a in accounts:
        default.setdefault(a.kind, a.id)

    def pick(kind: str, preferred):
        if preferred and kind_of.get(preferred) == kind:
            return preferred
        return default.get(kind)

    def move(account_id, amount):
        if account_id is None or amount == 0:
            return
        balances[account_id] += amount
        moves[account_id]["in" if amount > 0 else "out"] += abs(amount)

    query = db.query(models.Transaction.type, models.Transaction.amount,
                     models.Transaction.account_id, models.Transaction.direction)
    # Single-account model: when no separate savings account exists, Savings /
    # Withdrawal rows are internal cash bookkeeping and must NOT change the cash
    # balance (cash = opening + accumulated surplus). With a savings account
    # present they behave as transfers between cash and savings as before.
    has_savings_acct = any(a.kind == "savings" for a in accounts)
    for ttype, amount, account_id, direction in _in_range(query, None, as_of).all():
        amount = float(amount or 0.0)
        if ttype == models.TYPE_INCOME:
            move(pick("cash", account_id), amount)
        elif ttype == models.TYPE_EXPENSES:
            move(pick("cash", account_id), -amount)
        elif ttype == models.TYPE_SAVINGS:
            if has_savings_acct:
                move(pick("savings", account_id), amount)          # into savings ...
                move(pick("cash", None), -amount)                  # ... out of cash
        elif ttype == models.TYPE_WITHDRAWAL:
            if has_savings_acct:
                move(pick("savings", account_id), -amount)         # out of savings ...
                move(pick("cash", None), amount)                   # ... back into cash
        elif ttype == models.TYPE_LOAN:
            move(pick("cash", account_id),
                 amount if (direction or "borrow") == "borrow" else -amount)

    rows = [{
        "id": a.id, "name": a.name, "kind": a.kind,
        "opening_balance": float(a.opening_balance or 0.0),
        "opening_date": a.opening_date.isoformat() if a.opening_date else None,
        "note": a.note,
        "is_active": a.is_active,
        "movements_in": moves[a.id]["in"],
        "movements_out": moves[a.id]["out"],
        "balance": balances[a.id],
    } for a in accounts]
    return {
        "accounts": rows,
        "cash": sum(r["balance"] for r in rows if r["kind"] == "cash"),
        "savings": sum(r["balance"] for r in rows if r["kind"] == "savings"),
        "opening_total": sum(r["opening_balance"] for r in rows),
    }


def loan_balances(db: Session, as_of: Optional[date] = None) -> dict:
    """Outstanding per lender: everything borrowed minus everything repaid."""
    query = (db.query(models.Transaction.lender, models.Transaction.direction,
                      func.sum(models.Transaction.amount))
             .filter(models.Transaction.type == models.TYPE_LOAN))
    rows = _in_range(query, None, as_of).group_by(
        models.Transaction.lender, models.Transaction.direction).all()
    book: dict = {}
    for lender, direction, total in rows:
        name = lender or "Unnamed lender"
        entry = book.setdefault(name, {"lender": name, "borrowed": 0.0, "repaid": 0.0})
        if (direction or "borrow") == "repay":
            entry["repaid"] += float(total or 0.0)
        else:
            entry["borrowed"] += float(total or 0.0)
    loans = []
    for entry in book.values():
        entry["outstanding"] = entry["borrowed"] - entry["repaid"]
        loans.append(entry)
    loans.sort(key=lambda e: -e["outstanding"])
    return {"loans": loans, "total": sum(e["outstanding"] for e in loans),
            "total_borrowed": sum(e["borrowed"] for e in loans),
            "total_repaid": sum(e["repaid"] for e in loans)}


# --------------------------------------------------------------------- totals
def month_totals(db: Session) -> dict:
    """Per-month flows, separating the family's own entries from the automatic sweep."""
    buckets: dict = {}

    def bucket(month) -> dict:
        return buckets.setdefault(month, {
            "month": month, "income": 0.0, "expenses": 0.0,
            "savings_in": 0.0, "savings_in_loan": 0.0, "withdrawal": 0.0,
            "savings_auto": 0.0, "withdrawal_auto": 0.0,
            "loan_borrow": 0.0, "loan_repay": 0.0,
            "entries": 0, "manual_entries": 0,
        })

    query = (db.query(_month_expr(), models.Transaction.type, models.Transaction.direction,
                      models.Transaction.funded_by,
                      models.Transaction.auto_offset_month.isnot(None),
                      func.sum(models.Transaction.amount), func.count(models.Transaction.id))
             .group_by(_month_expr(), models.Transaction.type, models.Transaction.direction,
                       models.Transaction.funded_by,
                       models.Transaction.auto_offset_month.isnot(None)))
    for month, ttype, direction, funded_by, is_auto, total, n in query.all():
        e = bucket(month)
        amount = float(total or 0.0)
        e["entries"] += int(n or 0)
        if not is_auto:
            e["manual_entries"] += int(n or 0)
        if ttype == models.TYPE_INCOME:
            e["income"] += amount
        elif ttype == models.TYPE_EXPENSES:
            e["expenses"] += amount
        elif ttype == models.TYPE_SAVINGS:
            e["savings_in"] += amount
            if funded_by in NON_INCOME_FUNDING:
                e["savings_in_loan"] += amount
            if is_auto:
                e["savings_auto"] += amount
        elif ttype == models.TYPE_WITHDRAWAL:
            e["withdrawal"] += amount
            if is_auto:
                e["withdrawal_auto"] += amount
        elif ttype == models.TYPE_LOAN:
            if (direction or "borrow") == "repay":
                e["loan_repay"] += amount
            else:
                e["loan_borrow"] += amount

    for e in buckets.values():
        e["surplus"] = e["income"] - e["expenses"]
        e["savings_manual"] = e["savings_in"] - e["savings_in_loan"] - e["savings_auto"]
        e["withdrawal_manual"] = e["withdrawal"] - e["withdrawal_auto"]
        # what a month-end sweep still has to move for savings to track the surplus
        e["sweep_base"] = e["surplus"] - e["savings_manual"] + e["withdrawal_manual"]
        e["savings_net"] = e["savings_in"] - e["withdrawal"]
        e["net_worth_change"] = e["surplus"]
        e["cash_change"] = e["surplus"] - e["savings_net"] + e["loan_borrow"] - e["loan_repay"]
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


def apply_month_end_sweep(db: Session, today: Optional[date] = None) -> dict:
    """Move each closed month's surplus into savings — or take the deficit back out.

    ``base = surplus − savings already recorded that month + withdrawals``
    (transfers funded by a loan or an opening balance are ignored, and so are the
    sweep's own rows). ``base > 0`` posts a transfer *into* savings; ``base < 0``
    posts a **withdrawal**, i.e. that month's deficit was funded from savings.
    Idempotent: one row per month, matched by ``auto_offset_month``, updated in place.
    """
    today = today or date.today()
    current = month_of(today)
    totals = month_totals(db)
    created = updated = removed = 0
    changes = []
    category = None

    existing_rows = {
        t.auto_offset_month: t
        for t in db.query(models.Transaction)
        .filter(models.Transaction.auto_offset_month.isnot(None)).all()
    }

    for month in sorted(totals):
        if month >= current:
            continue                                    # still running, not closed
        entry = totals[month]
        row = existing_rows.get(month)
        if entry["manual_entries"] == 0:                 # month lost its real data
            if row is not None:
                db.delete(row)
                removed += 1
            continue

        base = round(entry["sweep_base"], 2)
        if abs(base) <= 0.005:
            if row is not None:
                db.delete(row)
                removed += 1
            continue

        if category is None:
            category = _saving_category(db)
        _, last = month_bounds(month)
        wanted_type = models.TYPE_SAVINGS if base > 0 else models.TYPE_WITHDRAWAL
        wanted_amount = abs(base)
        description = (f"Month-end sweep into savings for {month}" if base > 0 else
                       f"Month-end withdrawal from savings for {month} (the month ran a deficit)")

        if row is None:
            db.add(models.Transaction(
                date=last, type=wanted_type, category_id=category.id, amount=wanted_amount,
                description=description, auto_offset_month=month,
                member_id=None, created_by=None,
                funded_by="income" if base > 0 else None,
            ))
            changes.append({"month": month, "action": "created", "type": wanted_type,
                            "amount": wanted_amount})
            created += 1
        elif row.type != wanted_type or abs(float(row.amount) - wanted_amount) > 0.005:
            changes.append({"month": month, "action": "updated", "type": wanted_type,
                            "from_type": row.type, "from": float(row.amount), "to": wanted_amount})
            row.type = wanted_type
            row.amount = wanted_amount
            row.date = last
            row.description = description
            row.funded_by = "income" if base > 0 else None
            updated += 1

    if created or updated or removed:
        db.commit()
    return {"created": created, "updated": updated, "removed": removed,
            "closed_months": [m for m in sorted(totals) if m < current],
            "changes": changes}


# -------------------------------------------------------------------- savings
def savings_summary(db: Session, months: int = 12, today: Optional[date] = None) -> dict:
    """The savings book: month by month, per member, with the closing balance."""
    today = today or date.today()
    totals = month_totals(db)
    current = month_of(today)
    balances = account_balances(db)
    opening_savings = sum(r["opening_balance"] for r in balances["accounts"] if r["kind"] == "savings")

    per_member: dict = {}
    member_rows = (
        db.query(_month_expr(), _member_expr(), func.sum(models.Transaction.amount))
        .filter(models.Transaction.type == models.TYPE_SAVINGS,
                models.Transaction.auto_offset_month.is_(None),
                or_(models.Transaction.funded_by.is_(None),
                    models.Transaction.funded_by.notin_(NON_INCOME_FUNDING)))
        .group_by(_month_expr(), _member_expr()).all()
    )
    for month, member_id, total in member_rows:
        per_member.setdefault(month, {})[str(int(member_id) if member_id else 0)] = float(total or 0.0)

    members = [{"id": u.id, "name": u.full_name or u.username}
               for u in db.query(models.User).order_by(models.User.id).all()]

    selected = sorted(totals)[-months:] if months else sorted(totals)
    out = []
    running = opening_savings
    for m in sorted(totals):                     # walk every month so balances stay right
        e = totals[m]
        running += e["savings_net"]
        if m not in selected:
            continue
        out.append({
            "month": m, "closed": m < current,
            "income": e["income"], "expenses": e["expenses"], "surplus": e["surplus"],
            "savings_in": e["savings_in"], "savings_in_loan": e["savings_in_loan"],
            "withdrawal": e["withdrawal"],
            "sweep_in": e["savings_auto"], "sweep_out": e["withdrawal_auto"],
            "savings_net": e["savings_net"], "balance": running,
            "swept": bool(e["savings_auto"] or e["withdrawal_auto"]),
            "sweep_base": e["sweep_base"],
            "members": per_member.get(m, {}),
        })

    first_balance = out[0]["balance"] - out[0]["savings_net"] if out else opening_savings
    keys = ("income", "expenses", "surplus", "savings_in", "savings_in_loan",
            "withdrawal", "sweep_in", "sweep_out", "savings_net")
    grand = {k: sum(e[k] for e in out) for k in keys}
    grand.update({"opening_balance": first_balance, "closing_balance": running,
                  "opening_savings_total": opening_savings})
    return {"months": out, "members": members, "totals": grand,
            "current_month": current, "auto_sweep": True}


# ----------------------------------------------------------------- statements
def _period_flows(db: Session, start: Optional[date], end: Optional[date]) -> dict:
    query = (db.query(models.Transaction.type, models.Transaction.direction,
                      models.Transaction.funded_by, func.sum(models.Transaction.amount)))
    rows = _in_range(query, start, end).group_by(
        models.Transaction.type, models.Transaction.direction,
        models.Transaction.funded_by).all()
    out = {"income": 0.0, "expenses": 0.0, "savings_in": 0.0, "savings_in_loan": 0.0,
           "withdrawal": 0.0, "loan_borrow": 0.0, "loan_repay": 0.0}
    for ttype, direction, funded_by, total in rows:
        amount = float(total or 0.0)
        if ttype == models.TYPE_INCOME:
            out["income"] += amount
        elif ttype == models.TYPE_EXPENSES:
            out["expenses"] += amount
        elif ttype == models.TYPE_SAVINGS:
            out["savings_in"] += amount
            if funded_by in NON_INCOME_FUNDING:
                out["savings_in_loan"] += amount
        elif ttype == models.TYPE_WITHDRAWAL:
            out["withdrawal"] += amount
        elif ttype == models.TYPE_LOAN:
            if (direction or "borrow") == "repay":
                out["loan_repay"] += amount
            else:
                out["loan_borrow"] += amount
    return out


def income_statement(db: Session, start: Optional[date] = None,
                     end: Optional[date] = None) -> dict:
    """Income and expenses (the net-worth movements), with financing below the line."""
    rows = _in_range(
        db.query(models.Category.type, models.Category.group, models.Category.name,
                 func.sum(models.Transaction.amount))
        .join(models.Transaction, models.Transaction.category_id == models.Category.id)
        .filter(models.Transaction.type.in_([models.TYPE_INCOME, models.TYPE_EXPENSES])),
        start, end,
    ).group_by(models.Category.type, models.Category.group, models.Category.name).all()

    income_lines, expense_lines = [], []
    expense_groups: dict = {}
    for ctype, group, name, total in rows:
        amount = float(total or 0.0)
        if ctype == models.TYPE_INCOME:
            income_lines.append({"category": name, "group": group, "amount": amount})
        else:
            expense_lines.append({"category": name, "group": group, "amount": amount})
            expense_groups[group or "—"] = expense_groups.get(group or "—", 0.0) + amount
    income_lines.sort(key=lambda r: -r["amount"])
    expense_lines.sort(key=lambda r: -r["amount"])
    income_total = sum(r["amount"] for r in income_lines)
    expense_total = sum(r["amount"] for r in expense_lines)

    flows = _period_flows(db, start, end)
    savings_in = flows["savings_in"] - flows["savings_in_loan"]
    months = []
    first_month = month_of(start) if start else None
    last_month = month_of(end) if end else None
    for m, e in sorted(month_totals(db).items()):
        if (first_month and m < first_month) or (last_month and m > last_month):
            continue
        months.append({"month": m, "income": e["income"], "expenses": e["expenses"],
                       "surplus": e["surplus"], "savings": e["savings_net"]})

    return {
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "income_lines": income_lines, "income_total": income_total,
        "expense_lines": expense_lines, "expense_total": expense_total,
        "expense_groups": sorted(({"group": g, "amount": a} for g, a in expense_groups.items()),
                                 key=lambda r: -r["amount"]),
        "surplus": income_total - expense_total,
        # below the line: movements that shuffle money around without changing net worth
        "financing": {
            "savings_in": savings_in,
            "savings_in_funded_by_loan": flows["savings_in_loan"],
            "withdrawals": flows["withdrawal"],
            "loan_borrowed": flows["loan_borrow"],
            "loan_repaid": flows["loan_repay"],
            "savings_net": savings_in - flows["withdrawal"],
        },
        "months": months,
    }


def balance_sheet(db: Session, as_of: Optional[date] = None) -> dict:
    """Accounts with their movements, other assets, loans, and net worth."""
    accounts = account_balances(db, as_of)
    loans = loan_balances(db, as_of)
    flows = _period_flows(db, None, as_of)

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

    money_total = accounts["cash"] + accounts["savings"]
    items_total = sum(r["value"] for r in asset_rows)
    total_assets = money_total + items_total
    other_liabilities = sum(r["outstanding"] for r in liability_rows)
    total_liabilities = loans["total"] + other_liabilities
    net_worth = total_assets - total_liabilities

    surplus = flows["income"] - flows["expenses"]
    expected = accounts["opening_total"] + surplus

    by_kind: dict = {}
    for r in asset_rows:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0.0) + r["value"]

    return {
        "as_of": as_of.isoformat() if as_of else None,
        "accounts": accounts["accounts"],
        "cash_total": accounts["cash"],
        "savings_total": accounts["savings"],
        "money_total": money_total,
        "asset_items": asset_rows,
        "asset_items_total": items_total,
        "asset_items_by_kind": by_kind,
        "total_assets": total_assets,
        "loans": loans["loans"],
        "loans_total": loans["total"],
        "loans_borrowed": loans["total_borrowed"],
        "loans_repaid": loans["total_repaid"],
        "liability_items": liability_rows,
        "other_liabilities_total": other_liabilities,
        "liabilities_total": total_liabilities,
        "net_worth": net_worth,
        "from_activity": {"income": flows["income"], "expenses": flows["expenses"],
                          "surplus": surplus,
                          "savings_in": flows["savings_in"],
                          "withdrawals": flows["withdrawal"],
                          "loan_borrowed": flows["loan_borrow"],
                          "loan_repaid": flows["loan_repay"]},
        # the arithmetic identity this whole model rests on
        "reconciliation": {
            "opening_balances": accounts["opening_total"],
            "lifetime_surplus": surplus,
            "expected_net_worth": expected,
            "actual_net_worth": net_worth,
            "difference": round(net_worth - expected, 2),
        },
        "kinds": {"asset": ASSET_KINDS, "liability": LIABILITY_KINDS},
    }


def reconciliation(db: Session, start: Optional[date] = None,
                   end: Optional[date] = None) -> dict:
    """Month by month: does the money add up, and was every deficit funded?"""
    totals = month_totals(db)
    balances = account_balances(db)
    opening_savings = sum(r["opening_balance"] for r in balances["accounts"] if r["kind"] == "savings")
    opening_cash = sum(r["opening_balance"] for r in balances["accounts"] if r["kind"] == "cash")

    first = month_of(start) if start else None
    last = month_of(end) if end else None
    cash, savings = opening_cash, opening_savings
    rows = []
    for m in sorted(totals):
        e = totals[m]
        cash += e["cash_change"]
        savings += e["savings_net"]
        if (first and m < first) or (last and m > last):
            continue
        gap = -e["surplus"] if e["surplus"] < 0 else 0.0
        funded = e["withdrawal"] > 0 or e["loan_borrow"] > 0
        issue = None
        if gap > 0.5 and not funded:
            issue = "deficit with no recorded source — check for missing income"
        elif cash < -0.5:
            issue = "cash would go negative — a movement is missing"
        rows.append({
            "month": m, "closed": m < month_of(date.today()),
            "income": e["income"], "expenses": e["expenses"], "surplus": e["surplus"],
            "savings_in": e["savings_in"], "withdrawal": e["withdrawal"],
            "loan_borrow": e["loan_borrow"], "loan_repay": e["loan_repay"],
            "cash_change": e["cash_change"], "cash_balance": cash, "savings_balance": savings,
            "gap": gap, "funded": bool(funded), "issue": issue,
        })

    issues = [r for r in rows if r["issue"]]
    return {
        "rows": rows, "issues": issues,
        "identity": "income − expenses = Δcash + Δsavings + Δother assets − Δdebt",
        "totals": {
            "income": sum(r["income"] for r in rows),
            "expenses": sum(r["expenses"] for r in rows),
            "surplus": sum(r["surplus"] for r in rows),
            "savings_in": sum(r["savings_in"] for r in rows),
            "withdrawal": sum(r["withdrawal"] for r in rows),
            "loan_borrow": sum(r["loan_borrow"] for r in rows),
            "loan_repay": sum(r["loan_repay"] for r in rows),
            "months_with_issues": len(issues),
        },
    }
