import os
import csv
from datetime import datetime, date

from sqlalchemy.orm import Session

from . import models, crud, auth
from .database import SessionLocal

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")


def _ensure_master_admin(db: Session):
    """Create the master admin account if it does not exist yet."""
    master = db.query(models.User).filter(models.User.role == auth.ROLE_MASTER).first()
    if master:
        return
    existing = db.query(models.User).filter(models.User.username == "admin").first()
    if existing:
        existing.role = auth.ROLE_MASTER
        existing.is_active = True
        existing.is_approved = True
        db.commit()
        return
    user = models.User(
        username=os.getenv("MASTER_USERNAME", "admin"),
        email=os.getenv("MASTER_EMAIL", "admin@example.com"),
        full_name="Master Admin",
        hashed_password=auth.get_password_hash(os.getenv("MASTER_PASSWORD", "admin123")),
        role=auth.ROLE_MASTER,
        is_active=True,
        is_approved=True,
    )
    db.add(user)
    db.commit()


def _seed_categories(db: Session):
    if db.query(models.Category).count() > 0:
        return
    path = os.path.join(DATA_DIR, "seed_categories.csv")
    if not os.path.exists(path):
        return
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("name") or "").strip()
            if not name:
                continue
            db.add(models.Category(
                name=name,
                type=(row.get("type") or "Expenses").strip(),
                group=(row.get("group") or "").strip() or None,
                description=(row.get("description") or "").strip() or None,
            ))
    db.commit()


def _seed_transactions(db: Session):
    if db.query(models.Transaction).count() > 0:
        return
    path = os.path.join(DATA_DIR, "seed_transactions.csv")
    if not os.path.exists(path):
        return

    cats = {c.name.strip().lower(): c for c in db.query(models.Category).all()}

    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            cat_name = (row.get("category") or "").strip()
            cat = cats.get(cat_name.lower())
            if not cat:
                continue
            try:
                trx_date = datetime.strptime((row.get("date") or "").strip(), "%Y-%m-%d").date()
            except ValueError:
                continue
            try:
                amount = float(row.get("amount") or 0)
            except ValueError:
                amount = 0.0
            db.add(models.Transaction(
                date=trx_date,
                type=(row.get("type") or cat.type).strip(),
                category_id=cat.id,
                amount=amount,
                description=(row.get("description") or "").strip() or None,
            ))
    db.commit()


def seed_initial_data():
    db = SessionLocal()
    try:
        _ensure_master_admin(db)
        _seed_categories(db)
        _seed_transactions(db)
    finally:
        db.close()
