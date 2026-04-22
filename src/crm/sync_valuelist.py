"""同步 crm_ValueList → 本地 crm_value_list。

规模很小（~1K 行），直接全量覆盖。
"""

from __future__ import annotations

from datetime import datetime, timezone

from src import config
from src.crm import bytehouse, sync_state
from src.db.connection import connect, transaction


def sync() -> dict:
    scope = "crm_value_list"
    print("[sync_valuelist] start")

    ch = bytehouse.client()
    ods = config.BH_DATABASE_ODS

    rows = ch.execute(
        f"""
        SELECT
            id,
            customItem2 AS entity,
            customItem3 AS field,
            name        AS code,
            customItem1 AS label
        FROM {ods}.crm_ValueList
        """
    )

    now = datetime.now(timezone.utc).isoformat()
    conn = connect()
    try:
        with transaction(conn):
            conn.execute("DELETE FROM crm_value_list")
            conn.executemany(
                """
                INSERT INTO crm_value_list (id, entity, field, code, label, synced_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [(r[0], r[1], r[2], r[3], r[4], now) for r in rows],
            )
    finally:
        conn.close()

    sync_state.commit(scope, watermark=now, rows_last=len(rows), ok=True)
    print(f"[sync_valuelist] done  rows={len(rows)}")
    return {"rows": len(rows)}


if __name__ == "__main__":
    try:
        sync()
    except Exception as e:
        sync_state.commit("crm_value_list",
                          watermark=sync_state.get_watermark("crm_value_list"),
                          rows_last=0, ok=False, error=repr(e)[:500])
        raise
