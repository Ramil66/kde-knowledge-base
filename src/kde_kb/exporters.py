import json
from pathlib import Path

from sqlalchemy.engine import Connection

from .database import new_output, revision
from .models import metadata
from .services import rows


def export_json(conn: Connection, destination: Path) -> None:
    data = {table.name: rows(conn, table) for table in metadata.sorted_tables}
    with new_output(destination) as temp, temp.open("w", encoding="utf-8") as output:
        json.dump(
            {"schema_revision": revision(conn), "tables": data},
            output,
            ensure_ascii=False,
            indent=2,
        )
        output.write("\n")
