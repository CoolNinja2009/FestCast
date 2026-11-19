"""Database access. SQLite queries here are tiny (sub-millisecond) so they run inline on the event loop;
anything slow (file I/O, rendering, ffmpeg) is pushed to threads elsewhere."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .models import KV, Base

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


class Database:
    def __init__(self, path: Path | str, migrate: bool = True) -> None:
        self.url = f"sqlite:///{path}" if str(path) != ":memory:" else "sqlite://"
        self.engine = create_engine(self.url, connect_args={"check_same_thread": False})

        @event.listens_for(self.engine, "connect")
        def _pragma(conn, _rec):  # noqa: ANN001
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        self.Session = sessionmaker(self.engine, expire_on_commit=False)
        if migrate and self.url != "sqlite://":
            self.upgrade()
        else:
            Base.metadata.create_all(self.engine)

    def upgrade(self) -> None:
        """Apply Alembic migrations (never drops the database on update)."""
        from alembic import command
        from alembic.config import Config

        cfg = Config()
        cfg.set_main_option("script_location", str(MIGRATIONS))
        cfg.set_main_option("sqlalchemy.url", self.url)
        with self.engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self.Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def get(self, key: str, default: Any = None) -> Any:
        with self.session() as s:
            row = s.get(KV, key)
            return default if row is None else row.value

    def set(self, key: str, value: Any) -> None:
        with self.session() as s:
            row = s.get(KV, key)
            if row is None:
                s.add(KV(key=key, value=value))
            else:
                row.value = value
