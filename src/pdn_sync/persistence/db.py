"""Single-writer SQLite engine, PRAGMA enforcement, and Alembic upgrade helper."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[3]


def attach_sqlite_pragma_listener(engine: Engine) -> None:
    """Enforce PRAGMA foreign_keys=ON, journal_mode=WAL, and busy_timeout on connect."""

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
        has_autocommit = hasattr(dbapi_connection, "autocommit")
        prev_autocommit = getattr(dbapi_connection, "autocommit", None)
        if has_autocommit:
            dbapi_connection.autocommit = True
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()
            if has_autocommit and prev_autocommit is not None:
                dbapi_connection.autocommit = prev_autocommit


def create_sqlite_engine(db_path: Path) -> Engine:
    """Create a SQLite SQLAlchemy engine with single-writer WAL and foreign-key enforcement."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path.as_posix()}",
        future=True,
        connect_args={"timeout": 10.0},
    )
    attach_sqlite_pragma_listener(engine)
    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a typed SQLAlchemy Session factory."""
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def upgrade_database(db_path: Path) -> None:
    """Apply all Alembic migrations up to head for the given SQLite path."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    alembic_ini = REPO_ROOT / "alembic.ini"
    migrations_dir = REPO_ROOT / "migrations"
    cfg = AlembicConfig(str(alembic_ini))
    cfg.set_main_option("script_location", str(migrations_dir))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    command.upgrade(cfg, "head")
