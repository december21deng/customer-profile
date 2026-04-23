"""ingest_jobs 状态机读写。

状态迁移：
    queued → fetching → ingesting → extracting → committing → done
                                                           ↓
                                                         failed（任何阶段可跳）

失败是终态，但不阻止手动 regen 重跑（regen 会 `init` 一条新的同 record_id
 job，upsert 覆盖原行，attempts += 1）。
"""

from __future__ import annotations

from datetime import datetime

from src.db.connection import connect, transaction

VALID_STATES = {
    "queued", "fetching", "ingesting", "extracting", "committing",
    "done", "failed",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def init(record_id: str, customer_id: str) -> None:
    """起一条新 job（或把已存在的 job 重置为 queued，attempts += 1）。"""
    now = _now()
    conn = connect()
    try:
        with transaction(conn):
            # 已存在则 upsert：attempts + 1，状态回到 queued
            conn.execute(
                """
                INSERT INTO ingest_jobs
                    (record_id, customer_id, status, error, attempts,
                     started_at, updated_at, finished_at, cost_usd)
                VALUES (?, ?, 'queued', NULL, 1, ?, ?, NULL, NULL)
                ON CONFLICT(record_id) DO UPDATE SET
                    customer_id = excluded.customer_id,
                    status      = 'queued',
                    error       = NULL,
                    attempts    = ingest_jobs.attempts + 1,
                    started_at  = excluded.started_at,
                    updated_at  = excluded.updated_at,
                    finished_at = NULL,
                    cost_usd    = NULL
                """,
                (record_id, customer_id, now, now),
            )
    finally:
        conn.close()


def set_status(
    record_id: str,
    status: str,
    error: str | None = None,
    cost_usd: float | None = None,
) -> None:
    """更新状态。done/failed 自动写 finished_at。"""
    if status not in VALID_STATES:
        raise ValueError(f"invalid status {status!r}")

    now = _now()
    finished_at = now if status in ("done", "failed") else None

    # SQLite COALESCE：cost_usd=None 就保留原值
    conn = connect()
    try:
        with transaction(conn):
            conn.execute(
                """
                UPDATE ingest_jobs
                SET status      = ?,
                    error       = CASE
                                    WHEN ? IS NOT NULL THEN ?
                                    WHEN ? = 'failed'  THEN error
                                    ELSE NULL
                                  END,
                    cost_usd    = COALESCE(?, cost_usd),
                    updated_at  = ?,
                    finished_at = CASE
                                    WHEN ? IS NOT NULL THEN ?
                                    ELSE finished_at
                                  END
                WHERE record_id = ?
                """,
                (
                    status,
                    error, error,        # error 两次：判断 + 赋值
                    status,
                    cost_usd,
                    now,
                    finished_at, finished_at,
                    record_id,
                ),
            )
    finally:
        conn.close()


def get(record_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM ingest_jobs WHERE record_id = ?", (record_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
