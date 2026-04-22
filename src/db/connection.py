"""同步 sqlite3 连接 + WAL PRAGMA。

给 CRM sync CLI 和一次性脚本用。长连接的场合（FastAPI）用 aiosqlite 另起。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from src.config import APP_DB_PATH


def connect() -> sqlite3.Connection:
    """打开一个配置好 WAL + PRAGMA 的连接。"""
    conn = sqlite3.connect(
        APP_DB_PATH,
        timeout=10,
        isolation_level=None,  # 手动管理事务
        detect_types=sqlite3.PARSE_DECLTYPES,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE 写事务，异常自动回滚。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
