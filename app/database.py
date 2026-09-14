import logging
import os
import re
import time

from sqlalchemy import create_engine
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import sessionmaker, declarative_base

logger = logging.getLogger("familyfinancials.database")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./familyfinancials.db")

# Render/Heroku sometimes provide postgres:// but SQLAlchemy requires postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

BACKEND = "sqlite" if DATABASE_URL.startswith("sqlite") else "postgresql"
_PASSWORD_RE = re.compile(r"(://[^:/@]+:)[^@]*(@)")


def safe_url(url: str = None) -> str:
    """The connection URL with the password masked - safe to print in logs."""
    u = DATABASE_URL if url is None else url
    return _PASSWORD_RE.sub(r"\1***\2", u)


def describe_url(url: str = None) -> str:
    """Password-free summary of where the app is trying to connect."""
    u = DATABASE_URL if url is None else url
    try:
        from sqlalchemy.engine import make_url

        parsed = make_url(u)
        return (f"backend={parsed.get_backend_name()} host={parsed.host} "
                f"port={parsed.port} user={parsed.username} database={parsed.database}")
    except Exception as exc:  # malformed URI, missing driver, ...
        return f"URL could not be parsed ({type(exc).__name__}: {exc})"


def _create_engine(url: str, attempts: int = 5, delay: float = 3.0):
    """Build the engine and prove the database can be reached.

    Hosted free tiers (Render's free web service, a Neon database waking from
    suspension) may refuse the very first connection, so retry a few times
    before failing - and when it does fail, log one password-free line that
    says exactly what was wrong instead of a bare traceback.
    """
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            eng = create_engine(url, connect_args=connect_args, pool_pre_ping=True)
            with eng.connect():  # prove reachability now, not on the first request
                pass
            logger.info("Database ready [%s] on attempt %d/%d -> %s",
                        BACKEND, attempt, attempts, safe_url(url))
            return eng
        except Exception as exc:
            last_error = exc
            logger.error("DATABASE CONNECTION FAILED (attempt %d/%d) %s | %s: %s",
                         attempt, attempts, safe_url(url), type(exc).__name__, str(exc)[:400])
            if url.startswith("sqlite"):
                break  # a local file is not going to fix itself
            if isinstance(exc, (ArgumentError, ModuleNotFoundError, ImportError)) or isinstance(exc.__cause__, (ArgumentError, ModuleNotFoundError, ImportError)):
                break  # configuration mistake - retrying cannot help
            if attempt < attempts:
                time.sleep(delay)

    raise RuntimeError(
        "Could not connect to the database. {} | DATABASE_URL='{}' | last error: {}: {}".format(
            describe_url(url), safe_url(url), type(last_error).__name__, last_error)
        + " -- fix DATABASE_URL in your host's environment settings: it must be the full "
          "postgresql:// URI you copied from the database provider (including the password "
          "and ?sslmode=require), pasted without quotes or any 'psql' command in front of it."
    )


engine = _create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
