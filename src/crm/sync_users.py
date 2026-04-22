"""同步 crm_User__c → 本地 crm_users。

CRM 用户表是 `ODS_YL.crm_User__c_json`，结构是 (id, data, ...)。data 是 JSON。
我们用 CH 的 JSONExtract 抽关键字段，避免在 Python 里解析。
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from src import config
from src.crm import bytehouse, sync_state
from src.db.connection import connect, transaction

BATCH = 2000

UPSERT = """
INSERT INTO crm_users (id, display_name, dim_depart, created_at, updated_at, synced_at)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    display_name = excluded.display_name,
    dim_depart   = excluded.dim_depart,
    updated_at   = excluded.updated_at,
    synced_at    = excluded.synced_at
    -- feishu_open_id 不覆盖（手工映射）
"""


def _iso_to_ms(iso: str) -> int:
    from datetime import datetime
    s = iso.replace("Z", "+00:00")
    if "+" not in s and "T" in s:
        s = s + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return 0


def sync(*, init: bool = False) -> dict:
    scope = "crm_user"
    watermark = sync_state.EPOCH if init else sync_state.get_watermark(scope)
    print(f"[sync_users] start  init={init}  watermark={watermark}")

    ch = bytehouse.client()
    ods = config.BH_DATABASE_ODS

    total = 0
    max_ts = watermark
    while True:
        wm_ms = _iso_to_ms(max_ts)
        # 比较毫秒（CRM 的 updatedAt 就是 epoch millis），避免 DateTime64 overflow
        rows = ch.execute(
            f"""
            SELECT
                id,
                JSONExtractString(data, 'customItem1') AS display_name,
                JSONExtractString(data, 'dimDepart')   AS dim_depart,
                JSONExtractInt(data, 'createdAt')      AS created_ms,
                JSONExtractInt(data, 'updatedAt')      AS updated_ms
            FROM {ods}.crm_User__c_json
            WHERE is_crm_deleted = 0
              AND JSONExtractInt(data, 'updatedAt') > %(wm)s
            ORDER BY updated_ms ASC
            LIMIT {BATCH}
            """,
            {"wm": wm_ms},
        )
        if not rows:
            break

        now = datetime.now(timezone.utc).isoformat()
        conn = connect()
        try:
            with transaction(conn):
                for (uid, name, depart, created_ms, updated_ms) in rows:
                    created_iso = (
                        datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc).isoformat()
                        if created_ms else None
                    )
                    updated_iso = (
                        datetime.fromtimestamp(updated_ms / 1000, tz=timezone.utc).isoformat()
                        if updated_ms else None
                    )
                    conn.execute(
                        UPSERT,
                        (
                            uid,
                            name or None,
                            depart or None,
                            created_iso,
                            updated_iso,
                            now,
                        ),
                    )
                    if updated_iso and updated_iso > max_ts:
                        max_ts = updated_iso
        finally:
            conn.close()

        total += len(rows)
        print(f"[sync_users] batch: {len(rows)} rows, watermark → {max_ts}")
        if len(rows) < BATCH:
            break

    sync_state.commit(scope, watermark=max_ts, rows_last=total, ok=True)
    print(f"[sync_users] done  rows={total}  watermark={max_ts}")
    return {"rows": total, "watermark": max_ts}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--init", action="store_true")
    args = parser.parse_args()
    try:
        sync(init=args.init)
    except Exception as e:
        sync_state.commit("crm_user",
                          watermark=sync_state.get_watermark("crm_user"),
                          rows_last=0, ok=False, error=repr(e)[:500])
        raise


if __name__ == "__main__":
    main()
