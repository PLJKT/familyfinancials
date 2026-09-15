from datetime import datetime

from sqlalchemy import (
    Column, Integer, String, Float, Date, DateTime, Boolean, ForeignKey, Text
)
from sqlalchemy.orm import relationship

from .database import Base

# Transaction types
TYPE_INCOME = "Income"
TYPE_EXPENSES = "Expenses"
TYPE_SAVINGS = "Savings"          # transfer INTO savings (cash -> savings)
TYPE_WITHDRAWAL = "Withdrawal"    # money taken OUT of savings (savings -> cash)
TYPE_LOAN = "Loan"                # borrowing or repaying a loan
FLOW_TYPES = [TYPE_INCOME, TYPE_EXPENSES, TYPE_SAVINGS, TYPE_WITHDRAWAL, TYPE_LOAN]
# types that never touch income/expenses: they only move money between accounts
NEUTRAL_TYPES = [TYPE_SAVINGS, TYPE_WITHDRAWAL, TYPE_LOAN]

DIRECTIONS = {TYPE_LOAN: ["borrow", "repay"]}
# what funded a transfer into savings; 'loan' ones are excluded from the month-end sweep
FUNDED_BY = ["income", "loan", "opening", "other"]
ACCOUNT_KINDS = ["cash", "savings"]


class BackupLog(Base):
    """Audit trail of backup downloads / restores, used for the weekly reminder."""

    __tablename__ = "backup_logs"

    id = Column(Integer, primary_key=True, index=True)
    # export_excel | export_csv | import
    kind = Column(String(30), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    username = Column(String(80), nullable=True)
    row_count = Column(Integer, nullable=True)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(80), unique=True, index=True, nullable=False)
    email = Column(String(200), unique=True, index=True, nullable=False)
    full_name = Column(String(200), nullable=True)
    hashed_password = Column(String(255), nullable=False)
    # master_admin | admin | editor | viewer | downloader
    role = Column(String(30), nullable=False, default="viewer")
    is_active = Column(Boolean, default=False, nullable=False)
    is_approved = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    transactions = relationship(
        "Transaction", back_populates="creator", foreign_keys="Transaction.created_by"
    )
    savings_entries = relationship(
        "Transaction", back_populates="member", foreign_keys="Transaction.member_id"
    )


class Category(Base):
    __tablename__ = "categories"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), unique=True, index=True, nullable=False)
    type = Column(String(30), nullable=False)      # Income | Expenses | Savings
    group = Column(String(60), nullable=True)      # 收入 | 固定支出 | 弹性支出 | 储蓄
    description = Column(Text, nullable=True)

    transactions = relationship("Transaction", back_populates="category")


class Account(Base):
    """A money account the family holds, together with the balance it started from.

    The opening balance is a *stock*: it is deliberately not a transaction, so it
    never shows up as income, expense, saving or surplus — only in the balance sheet.
    """

    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), unique=True, index=True, nullable=False)
    kind = Column(String(20), nullable=False, default="cash")     # cash | savings
    opening_balance = Column(Float, nullable=False, default=0.0)
    opening_date = Column(Date, nullable=True)
    note = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    date = Column(Date, nullable=False, index=True)
    # Income | Expenses | Savings | Withdrawal | Loan
    type = Column(String(30), nullable=False)
    category_id = Column(Integer, ForeignKey("categories.id"), nullable=False)
    amount = Column(Float, nullable=False, default=0.0)
    description = Column(Text, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    # whose saving this is (family member); falls back to created_by when empty
    member_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    # account the movement lands in (savings account for Savings/Withdrawal, cash
    # account for Loan/Income/Expenses); empty = the default account of that kind
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=True, index=True)
    # borrow | repay, for Loan rows
    direction = Column(String(10), nullable=True)
    # lender name for Loan rows (e.g. the company)
    lender = Column(String(120), nullable=True)
    # for Savings rows: where the money came from (income | loan | opening | other)
    funded_by = Column(String(20), nullable=True)
    # set on the automatic month-end sweep rows, e.g. "2026-08"
    auto_offset_month = Column(String(7), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    category = relationship("Category", back_populates="transactions")
    creator = relationship("User", back_populates="transactions", foreign_keys=[created_by])
    member = relationship("User", back_populates="savings_entries", foreign_keys=[member_id])
    account = relationship("Account", foreign_keys=[account_id])


class AssetItem(Base):
    """User-entered asset that is not derived from transactions (property, vehicle, ...)."""

    __tablename__ = "asset_items"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    # property | vehicle | investment | cash | other
    kind = Column(String(40), nullable=False, default="other")
    value = Column(Float, nullable=False, default=0.0)
    acquired_on = Column(Date, nullable=True)
    note = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class LiabilityItem(Base):
    """User-entered debt that is *not* tracked as Loan transactions.

    Loans entered as transactions compute their outstanding balance automatically,
    so they must not be duplicated here. This is for everything else: a mortgage you
    do not want to itemise, credit cards, informal debts ...
    """

    __tablename__ = "liability_items"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    # mortgage | car_loan | personal_loan | credit_card | other
    kind = Column(String(40), nullable=False, default="other")
    outstanding = Column(Float, nullable=False, default=0.0)
    monthly_payment = Column(Float, nullable=True)
    interest_rate = Column(Float, nullable=True)    # annual, percent
    started_on = Column(Date, nullable=True)
    note = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
