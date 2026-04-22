"""sync_state 表的读写封装。"""

from __future__ import annotations

from datetime import datetime, timezone

from src.db.connection import connect


EPOCH = "1970-01-01T00:00:00"


def get_watermark(scope: str) -> str:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT watermark FROM sync_state WHERE scope = ?", (scope,)
        ).fetchone()
        return row["watermark"] if row else EPOCH
    finally:
        conn.close()


def commit(
    scope: str,
    *,
    watermark: str,
    rows_last: int,
    ok: bool = True,
    error: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO sync_state (scope, watermark, last_run_at, last_run_ok,
                                    last_error, rows_total, rows_last)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope) DO UPDATE SET
                watermark = excluded.watermark,
                last_run_at = excluded.last_run_at,
                last_run_ok = excluded.last_run_ok,
                last_error = excluded.last_error,
                rows_total = sync_state.rows_total + excluded.rows_last,
                rows_last = excluded.rows_last
            """,
            (scope, watermark, now, 1 if ok else 0, error, rows_last, rows_last),
        )
    finally:
        conn.close()


def rewind(scope: str, *, hours: int) -> None:
    """把 watermark 倒退 N 小时（对账后强同步用）。"""
    from datetime import timedelta

    conn = connect()
    try:
        row = conn.execute(
            "SELECT watermark FROM sync_state WHERE scope = ?", (scope,)
        ).fetchone()
        if not row:
            return
        ts = datetime.fromisoformat(row["watermark"].replace("Z", "+00:00"))
        new_ts = (ts - timedelta(hours=hours)).isoformat()
        conn.execute(
            "UPDATE sync_state SET watermark = ? WHERE scope = ?",
            (new_ts, scope),
        )
    finally:
        conn.close()
