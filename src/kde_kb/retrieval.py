import re

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .database import CatalogError
from .services import Record


def search(conn: Connection, query: str, limit: int = 20) -> list[Record]:
    tokens = re.findall(r"\w+", query)
    if not tokens or limit < 1:
        raise CatalogError("Enter a search term and a positive limit")
    statement = text("""
        SELECT kind, entity_id, snippet(search_index, 2, '[', ']', '…', 20) AS excerpt
          FROM search_index WHERE search_index MATCH :query
         ORDER BY rank, kind, entity_id LIMIT :limit
    """)
    expression = " AND ".join(f'"{token}"' for token in tokens)
    return [
        dict(row)
        for row in conn.execute(statement, {"query": expression, "limit": limit}).mappings()
    ]
