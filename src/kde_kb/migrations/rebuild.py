"""SQLite table replacement for revisions 0002 and 0003."""

import re

from alembic import op


def replace_columns(table: str, definitions: dict[str, tuple[str, str]]) -> None:
    db = op.get_bind()
    if (
        db.exec_driver_sql("PRAGMA foreign_keys").scalar()
        and db.exec_driver_sql(f"SELECT 1 FROM {table} LIMIT 1").first()
    ):
        raise ValueError("Table replacement requires the catalog migration transaction")
    objects = (
        db.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL",
            (table,),
        )
        .scalars()
        .all()
    )
    ddl = db.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).scalar_one()
    ddl = "CREATE TABLE rebuilt_catalog_table " + ddl[ddl.index("(") :]
    for field, (old, new) in definitions.items():
        ddl, count = re.subn(rf"\b{field} {old}(?=\s*,)", f"{field} {new}", ddl)
        if count != 1:
            raise ValueError(f"Unexpected schema for {table}.{field}")
    db.exec_driver_sql(ddl)
    db.exec_driver_sql(f"INSERT INTO rebuilt_catalog_table SELECT * FROM {table}")
    db.exec_driver_sql(f"DROP TABLE {table}")
    db.exec_driver_sql(f"ALTER TABLE rebuilt_catalog_table RENAME TO {table}")
    for sql in objects:
        db.exec_driver_sql(sql)
