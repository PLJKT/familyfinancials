"""Backup export / import engine.

The workbook produced by :func:`build_workbook_bytes` is the canonical backup
format of this system and MUST stay import-compatible.

Format
------
Sheet ``Transactions`` (required, header row on the first row that contains a
"Date" column)::

    Date | Type | Category | Amount | Description

Sheet ``Categories`` (optional, written by the exporter)::

    Category | Type | Group | Description

Compatibility rules:
  * Columns are matched by header name (case / spacing insensitive), so column
    order does not matter and the older 5-column export imports unchanged.
  * An extra ``ID`` column is tolerated and ignored (ids are always re-assigned
    on import, because the whole table is replaced).
  * Files can be ``.xlsx`` / ``.xlsm`` / ``.csv``.
  * Rows are fully validated BEFORE anything is written, so a bad file never
    destroys existing data.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

MAX_ROWS = 500_000

TYPE_INCOME = "Income"
TYPE_EXPENSES = "Expenses"
TYPE_SAVINGS = "Savings"
TYPE_WITHDRAWAL = "Withdrawal"
TYPE_LOAN = "Loan"
CANONICAL_TYPES = (TYPE_INCOME, TYPE_EXPENSES, TYPE_SAVINGS, TYPE_WITHDRAWAL, TYPE_LOAN, "Transfer")

_TYPE_ALIASES = {
    "income": TYPE_INCOME,
    "in": TYPE_INCOME,
    "revenue": TYPE_INCOME,
    "pemasukan": TYPE_INCOME,
    "收入": TYPE_INCOME,
    "expense": TYPE_EXPENSES,
    "expenses": TYPE_EXPENSES,
    "expenditure": TYPE_EXPENSES,
    "spending": TYPE_EXPENSES,
    "out": TYPE_EXPENSES,
    "pengeluaran": TYPE_EXPENSES,
    "支出": TYPE_EXPENSES,
    "固定支出": TYPE_EXPENSES,
    "弹性支出": TYPE_EXPENSES,
    "saving": TYPE_SAVINGS,
    "savings": TYPE_SAVINGS,
    "tabungan": TYPE_SAVINGS,
    "储蓄": TYPE_SAVINGS,
    "transfer": TYPE_SAVINGS,
    "transfer in": TYPE_SAVINGS,
    "转入": TYPE_SAVINGS,
    "withdrawal": TYPE_WITHDRAWAL,
    "withdraw": TYPE_WITHDRAWAL,
    "take out": TYPE_WITHDRAWAL,
    "消耗存款": TYPE_WITHDRAWAL,
    "取出": TYPE_WITHDRAWAL,
    "loan": TYPE_LOAN,
    "borrow": TYPE_LOAN,
    "borrowing": TYPE_LOAN,
    "借款": TYPE_LOAN,
    "repay": TYPE_LOAN,
    "repayment": TYPE_LOAN,
    "还款": TYPE_LOAN,
}

_HEADER_ALIASES = {
    "date": "date",
    "tanggal": "date",
    "日期": "date",
    "type": "type",
    "tipe": "type",
    "类型": "type",
    "category": "category",
    "categoryname": "category",
    "kategori": "category",
    "分类": "category",
    "amount": "amount",
    "value": "amount",
    "jumlah": "amount",
    "金额": "amount",
    "description": "description",
    "desc": "description",
    "note": "description",
    "notes": "description",
    "memo": "description",
    "keterangan": "description",
    "备注": "description",
    # optional fidelity columns (older files simply do not have them)
    "member": "member",
    "savedby": "member",
    "familymember": "member",
    "autooffset": "autooffset",
    "autooffsetmonth": "autooffset",
    "monthlyoffset": "autooffset",
    "direction": "direction",
    "borrowrepay": "direction",
    "lender": "lender",
    "lendername": "lender",
    "creditor": "lender",
    "fundedby": "fundedby",
    "source": "fundedby",
    "id": "id",
    "no": "id",
    "row": "id",
}

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
)

UNCATEGORIZED = "Uncategorized"


class BackupFormatError(Exception):
    """Raised when the uploaded file is not a usable backup."""

    def __init__(self, message: str, errors: Optional[List[Dict[str, Any]]] = None):
        super().__init__(message)
        self.message = message
        self.errors = errors or []


@dataclass
class ParsedRow:
    row_number: int
    date: date
    type: str
    category: str
    amount: float
    description: str
    member: Optional[str] = None            # username of the family member (optional column)
    auto_offset_month: Optional[str] = None  # "YYYY-MM" for automatic sweep rows
    direction: Optional[str] = None          # borrow | repay (Loan rows)
    lender: Optional[str] = None             # lender name (Loan rows)
    funded_by: Optional[str] = None          # income | loan | opening | other (Savings rows)


@dataclass
class ParsedBackup:
    sheet_name: str
    rows: List[ParsedRow] = field(default_factory=list)
    errors: List[Dict[str, Any]] = field(default_factory=list)
    categories: List[Dict[str, str]] = field(default_factory=list)
    total_data_rows: int = 0


# --------------------------------------------------------------------------- #
# cell parsing helpers
# --------------------------------------------------------------------------- #
def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def normalize_type(value: Any) -> Optional[str]:
    """Return one of Income / Expenses / Savings, or None if unrecognised."""
    text = _clean_text(value)
    if not text:
        return None
    return _TYPE_ALIASES.get(text.lower())


def parse_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _clean_text(value)
    if not text:
        raise ValueError("empty date")
    text = text.replace("T00:00:00", "")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date '{_clean_text(value)}' (expected YYYY-MM-DD)")


_NUMBER_NOISE = re.compile(r"[^0-9,.\-()]")


def parse_amount(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("invalid amount")
    if isinstance(value, (int, float)):
        return float(value)
    text = _clean_text(value)
    if not text:
        raise ValueError("empty amount")

    negative = text.startswith("-") or (text.startswith("(") and text.endswith(")"))
    text = _NUMBER_NOISE.sub("", text)
    text = text.strip("-()")
    if not text:
        raise ValueError("invalid amount")

    has_dot, has_comma = "." in text, "," in text
    if has_dot and has_comma:
        # the separator that appears last is the decimal separator
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif has_comma:
        head, _, tail = text.rpartition(",")
        if len(tail) in (1, 2) and "," not in head:
            text = head + "." + tail
        else:
            text = text.replace(",", "")
    elif has_dot and text.count(".") > 1:
        text = text.replace(".", "")

    try:
        number = float(text)
    except ValueError:
        raise ValueError(f"unrecognised amount '{_clean_text(value)}'")
    return -number if negative else number


def _normalize_header(name: Any) -> Optional[str]:
    key = re.sub(r"[\s_\-]+", "", _clean_text(name)).lower()
    return _HEADER_ALIASES.get(key)


def _clean_month(value: Any) -> Optional[str]:
    """Coerce a cell into 'YYYY-MM', tolerating datetime cells and '08/2026'."""
    text = _clean_text(value)
    if not text:
        return None
    text = text.strip()
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m")
    m = re.match(r"^(\d{4})[-/. ]?(\d{1,2})", text)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12:
            return f"{year:04d}-{month:02d}"
    m = re.match(r"^(\d{1,2})[-/. ](\d{4})$", text)
    if m:
        month, year = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12:
            return f"{year:04d}-{month:02d}"
    return None


# --------------------------------------------------------------------------- #
# table extraction
# --------------------------------------------------------------------------- #
def _read_xlsx(content: bytes) -> Tuple[str, List[List[Any]]]:
    try:
        import openpyxl
    except ImportError:  # pragma: no cover - dependency is in requirements
        raise BackupFormatError("openpyxl is not installed on the server")

    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001 - surfaced to the user
        raise BackupFormatError(f"Could not read the Excel file: {exc}")

    try:
        sheets: List[Tuple[str, List[List[Any]]]] = []
        for name in wb.sheetnames:
            rows = [list(r) for r in wb[name].iter_rows(values_only=True)]
            sheets.append((name, rows))
    finally:
        wb.close()

    for name, _ in sheets:  # prefer the canonical sheet
        if re.sub(r"[\s_]+", "", name).lower() in ("transactions", "transaction", "data"):
            rows = dict(sheets)[name]
            return name, rows
    for name, rows in sheets:  # otherwise any sheet with a Date header
        if _find_header_row(rows) is not None:
            return name, rows
    raise BackupFormatError(
        "No transaction sheet found. Expected a sheet named 'Transactions' with a "
        "'Date' column (the file produced by 'Download Excel backup')."
    )


def _read_csv(content: bytes) -> Tuple[str, List[List[Any]]]:
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover
        raise BackupFormatError("Could not decode the CSV file (tried UTF-8 and Latin-1)")

    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return "CSV", [list(r) for r in csv.reader(io.StringIO(text), dialect)]


def _find_header_row(rows: List[List[Any]]) -> Optional[int]:
    for idx, row in enumerate(rows[:25]):
        mapped = {_normalize_header(c) for c in row}
        if "date" in mapped and "amount" in mapped:
            return idx
    return None


def parse_backup_file(filename: str, content: bytes) -> ParsedBackup:
    """Parse an exported backup file. Read-only: never touches the database."""
    if not content:
        raise BackupFormatError("The uploaded file is empty")

    name = (filename or "").lower()
    if name.endswith(".csv"):
        sheet_name, rows = _read_csv(content)
    else:
        sheet_name, rows = _read_xlsx(content)

    header_idx = _find_header_row(rows)
    if header_idx is None:
        raise BackupFormatError(
            "Could not find a header row with 'Date' and 'Amount' columns in sheet "
            f"'{sheet_name}'."
        )

    headers = [_normalize_header(c) for c in rows[header_idx]]
    col = {key: i for i, key in enumerate(headers) if key and key not in ("id",)}

    missing = {"date", "type", "category", "amount"} - set(col)
    if missing:
        raise BackupFormatError(
            "Missing required column(s): " + ", ".join(sorted(missing)) +
            ". The file must have Date, Type, Category, Amount, Description."
        )

    def cell(row: List[Any], key: str) -> Any:
        idx = col.get(key)
        if idx is None or idx >= len(row):
            return None
        return row[idx]

    parsed = ParsedBackup(sheet_name=sheet_name)
    for offset, row in enumerate(rows[header_idx + 1:], start=header_idx + 2):
        if row is None or all(c is None or _clean_text(c) == "" for c in row):
            continue
        parsed.total_data_rows += 1
        if parsed.total_data_rows > MAX_ROWS:
            raise BackupFormatError(f"File has more than {MAX_ROWS:,} rows – refusing to import")

        try:
            row_date = parse_date(cell(row, "date"))
            row_type = normalize_type(cell(row, "type")) or TYPE_EXPENSES
            raw_category = _clean_text(cell(row, "category"))
            row_amount = parse_amount(cell(row, "amount"))
        except ValueError as exc:
            parsed.errors.append({"row": offset, "error": str(exc)})
            if len(parsed.errors) > 200:
                raise BackupFormatError("Too many malformed rows – import aborted", parsed.errors)
            continue

        parsed.rows.append(ParsedRow(
            row_number=offset,
            date=row_date,
            type=row_type,
            category=raw_category or UNCATEGORIZED,
            amount=row_amount,
            description=_clean_text(cell(row, "description")),
            member=_clean_text(cell(row, "member")) or None,
            auto_offset_month=_clean_month(cell(row, "autooffset")),
            direction=(_clean_text(cell(row, "direction")) or "").lower() or None,
            lender=_clean_text(cell(row, "lender")) or None,
            funded_by=(_clean_text(cell(row, "fundedby")) or "").lower() or None,
        ))

    # Optional Categories sheet: keeps type/group metadata on a full restore
    try:
        parsed.categories = _parse_categories_sheet(content) if not name.endswith(".csv") else []
    except Exception:  # noqa: BLE001 - metadata sheet is best-effort only
        parsed.categories = []

    return parsed


def _parse_categories_sheet(content: bytes) -> List[Dict[str, str]]:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    try:
        target = None
        for sheet in wb.sheetnames:
            if re.sub(r"[\s_]+", "", sheet).lower() in ("categories", "category", "kategori"):
                target = sheet
                break
        if target is None:
            return []
        rows = [list(r) for r in wb[target].iter_rows(values_only=True)]
    finally:
        wb.close()

    header_idx = 0
    for idx, row in enumerate(rows[:10]):
        if {_normalize_header(c) for c in row} & {"category"}:
            header_idx = idx
            break

    keys = {}
    for i, c in enumerate(rows[header_idx] if rows else []):
        n = _normalize_header(c)
        if n == "category":
            keys["name"] = i
        elif n == "type":
            keys["type"] = i
        elif n == "group":
            keys["group"] = i
        elif n == "description":
            keys["description"] = i
    if "name" not in keys:
        return []

    out: List[Dict[str, str]] = []
    for row in rows[header_idx + 1:]:
        if not row:
            continue
        name = _clean_text(row[keys["name"]]) if keys["name"] < len(row) else ""
        if not name:
            continue
        out.append({
            "name": name,
            "type": normalize_type(row[keys["type"]]) if "type" in keys and keys["type"] < len(row) else None,
            "group": _clean_text(row[keys["group"]]) if "group" in keys and keys["group"] < len(row) else "",
            "description": _clean_text(row[keys["description"]]) if "description" in keys and keys["description"] < len(row) else "",
        })
    return out


# --------------------------------------------------------------------------- #
# export builders
# --------------------------------------------------------------------------- #
def _member_label(transaction: Any) -> str:
    """Username of the family member a row belongs to (blank when unattributed)."""
    member = getattr(transaction, "member", None)
    if member is None:
        return ""
    return member.username or member.full_name or ""


def build_csv_text(transactions: Iterable[Any]) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Date", "Type", "Category", "Amount", "Description", "Member",
                     "AutoOffset", "Direction", "Lender", "FundedBy"])
    for t in transactions:
        writer.writerow([
            t.date.isoformat(),
            t.type,
            t.category.name if t.category else "",
            t.amount,
            t.description or "",
            _member_label(t),
            getattr(t, "auto_offset_month", None) or "",
            getattr(t, "direction", None) or "",
            getattr(t, "lender", None) or "",
            getattr(t, "funded_by", None) or "",
        ])
    return out.getvalue()


def build_workbook_bytes(transactions: Iterable[Any], categories: Iterable[Any]) -> bytes:
    """Canonical, re-importable backup workbook."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    transactions = list(transactions)
    categories = list(categories)

    wb = Workbook()
    ws = wb.active
    ws.title = "Transactions"
    ws.append(["Date", "Type", "Category", "Amount", "Description", "Member",
               "AutoOffset", "Direction", "Lender", "FundedBy"])
    for t in transactions:
        ws.append([
            t.date.isoformat(),
            t.type,
            t.category.name if t.category else "",
            t.amount,
            t.description or "",
            _member_label(t),
            getattr(t, "auto_offset_month", None) or "",
            getattr(t, "direction", None) or "",
            getattr(t, "lender", None) or "",
            getattr(t, "funded_by", None) or "",
        ])
    _style_sheet(ws, widths=(12, 12, 30, 16, 50, 14, 12, 10, 16, 12), money_col=4)

    ws2 = wb.create_sheet("Categories")
    ws2.append(["Category", "Type", "Group", "Description"])
    for c in categories:
        ws2.append([c.name, c.type, c.group or "", c.description or ""])
    _style_sheet(ws2, widths=(30, 12, 16, 50))

    ws3 = wb.create_sheet("Read me")
    for line in (
        "Family Financial Control – backup file",
        "",
        "This file is the system backup. Keep it safe and offline.",
        f"Transactions : {len(transactions)}",
        f"Categories   : {len(categories)}",
        "Created      : " + datetime.now().strftime("%Y-%m-%d %H:%M"),
        "",
        "HOW TO RESTORE",
        "  1. Log in as admin  ->  Admin page  ->  Backup & restore.",
        "  2. Upload this file and confirm 'replace all data'.",
        "",
        "RULES (so the file stays importable)",
        "  * Do not rename the 'Transactions' sheet.",
        "  * Keep the column headers in row 1: Date, Type, Category, Amount, Description.",
        "  * Date format: YYYY-MM-DD.  Type: Income / Expenses / Savings.",
    ):
        ws3.append([line])
    _style_sheet(ws3, widths=(90,))

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _style_sheet(ws, widths, money_col: Optional[int] = None) -> None:
    from openpyxl.styles import Alignment, Font

    for idx, width in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=1, column=idx).column_letter].width = width
    for cell in ws[1]:
        cell.font = Font(bold=True)
    ws.freeze_panes = "A2"
    if money_col:
        for cell in ws.iter_cols(min_col=money_col, max_col=money_col, min_row=2):
            for c in cell:
                c.number_format = "#,##0.00"
                c.alignment = Alignment(horizontal="right")
