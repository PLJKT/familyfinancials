from datetime import date, datetime
from typing import Optional, List
from pydantic import BaseModel, EmailStr, Field


# ---------- Auth / Users ----------
class UserBase(BaseModel):
    username: str
    email: EmailStr
    full_name: Optional[str] = None


class UserCreate(UserBase):
    password: str = Field(min_length=6)


class UserAdminCreate(UserCreate):
    """Admin-created account: active and approved straight away."""

    role: str = "viewer"
    is_active: bool = True
    is_approved: bool = True


class PasswordReset(BaseModel):
    new_password: str = Field(min_length=6)


class UserOut(UserBase):
    id: int
    role: str
    is_active: bool
    is_approved: bool
    created_at: datetime

    class Config:
        from_attributes = True


class UserUpdate(BaseModel):
    role: Optional[str] = None
    is_active: Optional[bool] = None
    is_approved: Optional[bool] = None
    full_name: Optional[str] = None


class LoginRequest(BaseModel):
    username: str
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


# ---------- Categories ----------
class CategoryBase(BaseModel):
    name: str
    type: str
    group: Optional[str] = None
    description: Optional[str] = None


class CategoryCreate(CategoryBase):
    pass


class CategoryOut(CategoryBase):
    id: int

    class Config:
        from_attributes = True


# ---------- Transactions ----------
class TransactionBase(BaseModel):
    date: date
    type: str
    category_id: int
    amount: float
    description: Optional[str] = None


class TransactionCreate(TransactionBase):
    # whose saving this is (family member); defaults to the author when omitted
    member_id: Optional[int] = None


class TransactionUpdate(BaseModel):
    date: Optional[date] = None
    type: Optional[str] = None
    category_id: Optional[int] = None
    amount: Optional[float] = None
    description: Optional[str] = None
    member_id: Optional[int] = None


class TransactionOut(TransactionBase):
    id: int
    created_by: Optional[int] = None
    member_id: Optional[int] = None
    auto_offset_month: Optional[str] = None
    created_at: datetime
    category: CategoryOut

    class Config:
        from_attributes = True


# ---------- Savings ----------
class SavingsEntryCreate(BaseModel):
    """A saving contribution recorded from the Savings page."""

    date: date
    amount: float
    member_id: Optional[int] = None      # whose saving; defaults to the author
    note: Optional[str] = None
    category_id: Optional[int] = None    # defaults to the 'Saving' category


# ---------- Balance sheet: assets / liabilities ----------
class AssetItemBase(BaseModel):
    name: str
    kind: str = "other"                  # property | vehicle | investment | cash | other
    value: float = 0.0
    acquired_on: Optional[date] = None
    note: Optional[str] = None
    is_active: bool = True


class AssetItemCreate(AssetItemBase):
    pass


class AssetItemUpdate(BaseModel):
    name: Optional[str] = None
    kind: Optional[str] = None
    value: Optional[float] = None
    acquired_on: Optional[date] = None
    note: Optional[str] = None
    is_active: Optional[bool] = None


class AssetItemOut(AssetItemBase):
    id: int

    class Config:
        from_attributes = True


class LiabilityItemBase(BaseModel):
    name: str
    kind: str = "other"                  # mortgage | car_loan | personal_loan | credit_card | other
    outstanding: float = 0.0
    monthly_payment: Optional[float] = None
    interest_rate: Optional[float] = None
    started_on: Optional[date] = None
    note: Optional[str] = None
    is_active: bool = True


class LiabilityItemCreate(LiabilityItemBase):
    pass


class LiabilityItemUpdate(BaseModel):
    name: Optional[str] = None
    kind: Optional[str] = None
    outstanding: Optional[float] = None
    monthly_payment: Optional[float] = None
    interest_rate: Optional[float] = None
    started_on: Optional[date] = None
    note: Optional[str] = None
    is_active: Optional[bool] = None


class LiabilityItemOut(LiabilityItemBase):
    id: int

    class Config:
        from_attributes = True


class OffsetRunResult(BaseModel):
    created: int
    updated: int
    removed: int
    closed_months: List[str] = []


# ---------- Reports ----------
class ReportQuery(BaseModel):
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    category_ids: Optional[List[int]] = None
    types: Optional[List[str]] = None
    group_by: str = "month"   # month | year | category


class SummaryRow(BaseModel):
    key: str
    income: float = 0.0
    expenses: float = 0.0
    savings: float = 0.0
    net: float = 0.0


class SummaryResponse(BaseModel):
    rows: List[SummaryRow]
    totals: SummaryRow


# ---------- Backup / restore ----------
class BackupLogOut(BaseModel):
    id: int
    kind: str
    username: Optional[str] = None
    row_count: Optional[int] = None
    note: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class BackupStatus(BaseModel):
    interval_days: int
    due: bool
    days_since_last_backup: Optional[float] = None
    last_backup_at: Optional[datetime] = None
    last_backup_kind: Optional[str] = None
    last_import_at: Optional[datetime] = None
    transaction_count: int
    persistent_storage: bool = True
    storage_note: Optional[str] = None
    # admin-only diagnostics (never exposed on the public /healthz)
    database_ok: Optional[bool] = None
    database_error: Optional[str] = None
    database_target: Optional[str] = None
    app_version: Optional[str] = None
    started_at: Optional[str] = None
    history: List[BackupLogOut] = []


class ImportResult(BaseModel):
    ok: bool
    sheet: str
    imported: int
    deleted: int
    categories_created: int
    categories_updated: int
    rows_read: int
    skipped_rows: List[dict] = []
    warnings: List[str] = []

