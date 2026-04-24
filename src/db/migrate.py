"""`python -m src.db.migrate` - 建表/补表（幂等）。"""

from __future__ import annotations

import sqlite3

from src.db.connection import connect
from src.db.schema import SCHEMA


# 破坏性或补列的手写迁移，按顺序跑。
# 必须幂等：用 PRAGMA table_info 判断是否已经有列。
def _add_col_if_missing(conn: sqlite3.Connection, table: str, col: str, decl: str) -> bool:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if col in cols:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    return True


def _run_column_migrations(conn: sqlite3.Connection) -> int:
    """v0.3: followup_records 扩列（手动录入字段）。"""
    added = 0
    # 只在表已经存在时补列；新库由 SCHEMA 的 CREATE TABLE 一次性建好。
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='followup_records'"
    ).fetchone()
    if has_table:
        for col, decl in [
            ("location", "TEXT"),
            ("our_attendees", "TEXT"),
            ("client_attendees", "TEXT"),
            ("background", "TEXT"),
            ("minutes_doc_url", "TEXT"),
            ("minutes_doc_id", "TEXT"),
            ("transcript_url", "TEXT"),
            ("photo_image_key", "TEXT"),
            # v0.4: 列表 item 新加的两个 AI 短字段
            ("meeting_title", "TEXT NOT NULL DEFAULT ''"),
            ("progress_line", "TEXT NOT NULL DEFAULT ''"),
            # v0.5: 多图：JSON 数组
            ("photo_image_keys", "TEXT NOT NULL DEFAULT '[]'"),
            # v0.7: 从 docx 里 ingest 的画板/图片（非销售手动上传）
            ("minutes_media", "TEXT NOT NULL DEFAULT '[]'"),
            # v0.8: 其他人员 + 会议结束时间
            ("other_attendees", "TEXT"),
            ("meeting_end_time", "TEXT"),
        ]:
            if _add_col_if_missing(conn, "followup_records", col, decl):
                added += 1

    # v0.6: user_tokens 加 display_name + avatar（OAuth 时顺手存）
    has_ut = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_tokens'"
    ).fetchone()
    if has_ut:
        for col, decl in [
            ("display_name", "TEXT NOT NULL DEFAULT ''"),
            ("avatar", "TEXT NOT NULL DEFAULT ''"),
        ]:
            if _add_col_if_missing(conn, "user_tokens", col, decl):
                added += 1
    return added


def migrate() -> None:
    conn = connect()
    try:
        for stmt in SCHEMA:
            conn.execute(stmt)
        added = _run_column_migrations(conn)
        db_file = conn.execute("PRAGMA database_list").fetchone()["file"]
        print(
            f"[migrate] {len(SCHEMA)} statements applied to {db_file}"
            + (f"; +{added} columns" if added else "")
        )
    finally:
        conn.close()


if __name__ == "__main__":
    migrate()
