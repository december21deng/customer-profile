"""SQLite store for thread_id → session_id mapping."""

import aiosqlite
from datetime import datetime, timezone

from src.config import DB_PATH

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS thread_sessions (
    thread_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_active_at TEXT NOT NULL
)
"""


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(_CREATE_TABLE)
        await db.commit()


async def get_session(thread_id: str) -> tuple[str, str] | None:
    """Returns (session_id, environment_id) or None."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT session_id, environment_id FROM thread_sessions WHERE thread_id = ?",
            (thread_id,),
        )
        row = await cursor.fetchone()
        if row:
            await db.execute(
                "UPDATE thread_sessions SET last_active_at = ? WHERE thread_id = ?",
                (_now(), thread_id),
            )
            await db.commit()
            return row[0], row[1]
        return None


async def save_session(thread_id: str, session_id: str, environment_id: str) -> None:
    now = _now()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO thread_sessions (thread_id, session_id, environment_id, created_at, last_active_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(thread_id) DO UPDATE SET
                 session_id = excluded.session_id,
                 environment_id = excluded.environment_id,
                 last_active_at = excluded.last_active_at""",
            (thread_id, session_id, environment_id, now, now),
        )
        await db.commit()


async def delete_session(thread_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM thread_sessions WHERE thread_id = ?", (thread_id,))
        await db.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
