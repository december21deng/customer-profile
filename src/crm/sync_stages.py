"""同步每个客户最近一次 saleStageId。

数据源：DWD_YL.crm_activityrecord
关键：activityRecordFrom = 11 表示 activity 关联到 account；
     activityRecordFrom_data 是 account id。
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone  # noqa: F401

from src import config
from src.crm import bytehouse, sync_state
from src.db.connection import connect, transaction

BATCH = 10000


def _iso_to_ms(iso: str) -> int:
    s = iso.replace("Z", "+00:00")
    if "+" not in s and "T" in s:
        s = s + "+00:00"
    try:
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except ValueError:
        return 0


def sync(*, init: bool = False) -> dict:
    """对最近有活动的客户，更新 crm_sale_stage_id。

    init=True 会回溯到 epoch，拉所有客户的最新 stage（4.9M 记录聚合）。
    增量模式：只处理 createdAt > watermark 的活动。
    """
    scope = "crm_stage"
    watermark = sync_state.EPOCH if init else sync_state.get_watermark(scope)
    print(f"[sync_stages] start  init={init}  watermark={watermark}")

    ch = bytehouse.client()
    ods = config.BH_DATABASE_ODS

    # DWD 版 crm_activityrecord 是空的。从 ODS json 表里用 JSONExtract 聚合。
    # activityRecordFrom=11 → 关联 account；activityRecordFrom_data 是 account id。
    rows = ch.execute(
        f"""
        SELECT
            JSONExtractString(data, 'activityRecordFrom_data') AS account_id,
            argMax(JSONExtractString(data, 'saleStageId'), JSONExtractInt(data, 'createdAt')) AS latest_stage_id,
            max(JSONExtractInt(data, 'createdAt')) AS latest_ms
        FROM {ods}.crm_activityrecord_json
        WHERE is_crm_deleted = 0
          AND JSONExtractInt(data, 'activityRecordFrom') = 11
          AND JSONExtractString(data, 'saleStageId') != ''
          AND JSONExtractInt(data, 'createdAt') > %(wm_ms)s
        GROUP BY account_id
        """,
        {"wm_ms": _iso_to_ms(watermark)},
    )

    if not rows:
        print("[sync_stages] no new activities")
        sync_state.commit(scope, watermark=watermark, rows_last=0, ok=True)
        return {"rows": 0}

    max_ts = watermark
    updated = 0
    conn = connect()
    try:
        with transaction(conn):
            for (account_id, stage_id, latest_ms) in rows:
                r = conn.execute(
                    "UPDATE customers SET crm_sale_stage_id = ? WHERE id = ?",
                    (stage_id, account_id),
                )
                updated += r.rowcount
                if latest_ms:
                    iso = datetime.fromtimestamp(latest_ms / 1000, tz=timezone.utc).isoformat()
                    if iso > max_ts:
                        max_ts = iso
    finally:
        conn.close()

    sync_state.commit(scope, watermark=max_ts, rows_last=updated, ok=True)
    print(f"[sync_stages] done  updated={updated}  watermark={max_ts}")
    return {"rows": updated, "watermark": max_ts}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--init", action="store_true")
    args = parser.parse_args()
    try:
        sync(init=args.init)
    except Exception as e:
        sync_state.commit("crm_stage",
                          watermark=sync_state.get_watermark("crm_stage"),
                          rows_last=0, ok=False, error=repr(e)[:500])
        raise


if __name__ == "__main__":
    main()
