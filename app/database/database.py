from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.database.models import Base


def make_engine(db_path: Path, readonly: bool = False) -> Engine:
    if readonly:
        # Dry-run must not create or touch the database file.
        engine = create_engine(f"sqlite:///file:{db_path}?mode=ro&uri=true", future=True)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(f"sqlite:///{db_path}", future=True)

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        if not readonly:
            # Rollback journal rather than WAL: a read-only (dry-run) open then leaves no -wal/-shm files.
            cur.execute("PRAGMA journal_mode=DELETE")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.close()

    return engine


class Database:
    def __init__(self, db_path: Path, readonly: bool = False):
        self.path = db_path
        self.readonly = readonly
        self.engine = make_engine(db_path, readonly)
        self._factory = sessionmaker(self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)
        self._add_missing_columns()

    def _add_missing_columns(self) -> None:
        """Minimal forward migration: new nullable columns are added to existing tables."""
        insp = inspect(self.engine)
        with self.engine.begin() as conn:
            for table in Base.metadata.sorted_tables:
                existing = {c["name"] for c in insp.get_columns(table.name)}
                for col in table.columns:
                    if col.name not in existing:
                        ddl = col.type.compile(dialect=self.engine.dialect)
                        conn.exec_driver_sql(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {ddl}')

    @contextmanager
    def session(self) -> Iterator[Session]:
        """One transaction: commits on success, rolls back on any error."""
        s = self._factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def dispose(self) -> None:
        self.engine.dispose()
