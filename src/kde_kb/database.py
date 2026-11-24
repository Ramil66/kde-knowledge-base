from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from importlib.resources import files
from pathlib import Path
from tempfile import NamedTemporaryFile

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL, Connection


class CatalogError(ValueError):
    """An actionable catalog error suitable for interactive clients."""


def default_path() -> Path:
    root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    return root / "kde-kb" / "catalog.sqlite3"


def config() -> Config:
    settings = Config()
    settings.set_main_option("script_location", str(files("kde_kb.migrations")).replace("%", "%%"))
    return settings


@contextmanager
def transaction(
    path: Path, *, write: bool = False, migrate: bool = False, check_revision: bool = True
) -> Iterator[Connection]:
    path = path.expanduser()
    if migrate:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.is_file():
        raise CatalogError("Database does not exist. Run kde-kb init first.")
    engine = create_engine(URL.create("sqlite", database=str(path)), future=True)

    @event.listens_for(engine, "connect")
    def configure(dbapi: sqlite3.Connection, _record: object) -> None:
        dbapi.isolation_level = None
        dbapi.execute("PRAGMA foreign_keys=OFF" if migrate else "PRAGMA foreign_keys=ON")

    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("BEGIN IMMEDIATE" if write or migrate else "BEGIN")
            if check_revision and not migrate and revision(conn) != head():
                raise CatalogError("Database revision is out of date. Run kde-kb db migrate.")
            yield conn
            if migrate and conn.exec_driver_sql("PRAGMA foreign_key_check").first():
                raise CatalogError("Migration failed referential integrity checks")
    finally:
        engine.dispose()


def revision(conn: Connection) -> str | None:
    return MigrationContext.configure(conn).get_current_revision()


def head() -> str | None:
    return ScriptDirectory.from_config(config()).get_current_head()


def migrate_database(path: Path) -> None:
    with transaction(path, migrate=True) as conn:
        settings = config()
        settings.attributes["connection"] = conn
        command.upgrade(settings, "head")


@contextmanager
def new_output(destination: Path) -> Iterator[Path]:
    """Publish only a complete file and never replace an existing one."""
    with NamedTemporaryFile(dir=destination.parent, delete=False) as tmp:
        temporary = Path(tmp.name)
    try:
        yield temporary
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def backup(path: Path, destination: Path) -> None:
    with (
        transaction(path) as conn,
        new_output(destination) as temporary,
        closing(sqlite3.connect(temporary)) as target,
    ):
        conn.connection.backup(target)
