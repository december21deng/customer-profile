"""`python -m src.db.migrate` - 建表/补表（幂等）。"""

from __future__ import annotations

from src.db.connection import connect
from src.db.schema import SCHEMA


def migrate() -> None:
    conn = connect()
    try:
        for stmt in SCHEMA:
            conn.execute(stmt)
        print(f"[migrate] {len(SCHEMA)} statements applied to {conn.execute('PRAGMA database_list').fetchone()['file']}")
    finally:
        conn.close()


if __name__ == "__main__":
    migrate()
