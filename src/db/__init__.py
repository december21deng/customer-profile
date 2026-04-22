"""v0.2 应用数据库（SQLite + WAL）。

同步 CLI 和 H5 API 都走这个 DB。sync 用 stdlib sqlite3，H5 后续用 aiosqlite。
两者共用同一个文件，靠 WAL 保证读不阻塞写。
"""

from .connection import connect, transaction
from .migrate import migrate

__all__ = ["connect", "transaction", "migrate"]
