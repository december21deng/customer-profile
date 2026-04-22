"""SQLite store mapping Lark thread_id -> Claude Agent SDK session_id."""

import aiosqlite
from datetime import datetime, timezone

from src.config import DB_PATH

_CREATE_SESSIONS = """
CREATE TABLE IF NOT EXISTS thread_sessions (
    thread_id TEXT PRIMARY KEY,
    session_id TEXT,
    updated_at TEXT NOT NULL
)
"""

_CREATE_MIAOJI = """
CREATE TABLE IF NOT EXISTS miaoji (
    token TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT,
    customer_name TEXT,
    thread_id TEXT,
    ingested_at TEXT NOT NULL
)
"""

_CREATE_DOCUMENTS = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT,
    thread_id TEXT,
    ingested_at TEXT NOT NULL
)
"""


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(_CREATE_SESSIONS)
        await db.execute(_CREATE_MIAOJI)
        await db.execute(_CREATE_DOCUMENTS)
        await db.commit()


async def get_session_id(thread_id: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT session_id FROM thread_sessions WHERE thread_id = ?",
            (thread_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row and row[0] else None


async def save_session_id(thread_id: str, session_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO thread_sessions (thread_id, session_id, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(thread_id) DO UPDATE SET
                 session_id = excluded.session_id,
                 updated_at = excluded.updated_at""",
            (thread_id, session_id, now),
        )
        await db.commit()


async def miaoji_exists(token: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT 1 FROM miaoji WHERE token = ?", (token,)
        )
        return await cursor.fetchone() is not None


async def save_miaoji(
    token: str,
    url: str,
    title: str | None,
    customer_name: str | None,
    thread_id: str,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO miaoji
               (token, url, title, customer_name, thread_id, ingested_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (token, url, title, customer_name, thread_id, now),
        )
        await db.commit()


async def document_exists(doc_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT 1 FROM documents WHERE doc_id = ?", (doc_id,)
        )
        return await cursor.fetchone() is not None


async def save_document(
    doc_id: str,
    url: str,
    title: str | None,
    thread_id: str,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO documents
               (doc_id, url, title, thread_id, ingested_at)
               VALUES (?, ?, ?, ?, ?)""",
            (doc_id, url, title, thread_id, now),
        )
        await db.commit()
