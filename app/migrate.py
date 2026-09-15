"""Forward-only schema patcher.

`Base.metadata.create_all()` creates missing *tables* but never adds a *column*
to a table that already exists, so every new column needs an explicit ALTER
TABLE. Keeping them here lets the live database upgrade itself on deploy.
"""
import logging

from sqlalchemy import inspect, text

logger = logging.getLogger("familyfinancials.migrate")

# (table, column, DDL type)
ADDED_COLUMNS = [
    ("transactions", "member_id", "INTEGER"),
    ("transactions", "auto_offset_month", "VARCHAR(7)"),
    ("transactions", "account_id", "INTEGER"),
    ("transactions", "direction", "VARCHAR(10)"),
    ("transactions", "lender", "VARCHAR(120)"),
    ("transactions", "funded_by", "VARCHAR(20)"),
]

INDEXES = [
    ("ix_transactions_member_id", "transactions", "member_id"),
    ("ix_transactions_auto_offset_month", "transactions", "auto_offset_month"),
    ("ix_transactions_account_id", "transactions", "account_id"),
]


def _columns(engine, table: str) -> set:
    return {c["name"] for c in inspect(engine).get_columns(table)}


def _indexes(engine, table: str) -> set:
    try:
        return {i["name"] for i in inspect(engine).get_indexes(table)}
    except Exception:
        return set()


def ensure_schema(engine) -> list:
    """Add any missing column/index; returns what was applied."""
    tables = set(inspect(engine).get_table_names())
    applied = []

    for table, column, ddl in ADDED_COLUMNS:
        if table not in tables:
            continue  # create_all() builds it with the column already in place
        if column in _columns(engine, table):
            continue
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
        applied.append(f"{table}.{column}")
        logger.info("Schema upgrade: added column %s.%s", table, column)

    for index_name, table, column in INDEXES:
        if table not in tables or index_name in _indexes(engine, table):
            continue
        try:
            with engine.begin() as conn:
                conn.execute(text(f"CREATE INDEX {index_name} ON {table} ({column})"))
            applied.append(index_name)
        except Exception as exc:  # an index is a nicety, never fatal
            logger.warning("Could not create index %s: %s", index_name, exc)

    if applied:
        logger.info("Schema upgrade applied: %s", ", ".join(applied))
    return applied
