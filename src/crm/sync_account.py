"""同步 ODS_YL.crm_account → 本地 customers。

- `--init`：从 epoch 开始全量
- 默认：增量，按 updatedAt > watermark
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone
from typing import Any, Sequence

from src import config
from src.crm import bytehouse, sync_state
from src.db.connection import connect, transaction

log = logging.getLogger("sync_account")

BATCH = 5000

# CRM 字段白名单（顺序重要，要与下面 UPSERT 的 binding 一致）
FIELDS = [
    "id",
    "accountName",
    "ownerId",
    "level",
    "industryId",
    "dimDepart",
    "isCustomer",
    "is_crm_deleted",
    "recentActivityRecordTime",
    "totalOrderAmount",
    "accountScore",
    "state",
    "sharedTags",
    "accountChannel",
    "parentAccountId",
    "createdAt",
    "updatedAt",
]


UPSERT = """
INSERT INTO customers (
    id, name,
    crm_owner_id, crm_level, crm_industry_id, crm_dim_depart,
    crm_is_customer, crm_is_deleted, crm_recent_activity_at,
    crm_total_order_amount, crm_account_score, crm_state, crm_shared_tags,
    crm_channel, crm_parent_id, crm_created_at, crm_updated_at,
    summary, wiki_path, local_updated_at, synced_at
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', NULL, ?)
ON CONFLICT(id) DO UPDATE SET
    name                   = excluded.name,
    crm_owner_id           = excluded.crm_owner_id,
    crm_level              = excluded.crm_level,
    crm_industry_id        = excluded.crm_industry_id,
    crm_dim_depart         = excluded.crm_dim_depart,
    crm_is_customer        = excluded.crm_is_customer,
    crm_is_deleted         = excluded.crm_is_deleted,
    crm_recent_activity_at = excluded.crm_recent_activity_at,
    crm_total_order_amount = excluded.crm_total_order_amount,
    crm_account_score      = excluded.crm_account_score,
    crm_state              = excluded.crm_state,
    crm_shared_tags        = excluded.crm_shared_tags,
    crm_channel            = excluded.crm_channel,
    crm_parent_id          = excluded.crm_parent_id,
    crm_updated_at         = excluded.crm_updated_at,
    synced_at              = excluded.synced_at
    -- 本地字段 summary / wiki_path / local_updated_at / crm_created_at 不覆盖
"""


def _to_float(x: Any) -> float | None:
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _to_int(x: Any) -> int | None:
    if x is None or x == "":
        return None
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def _iso_to_ms(iso: str) -> int:
    s = iso.replace("Z", "+00:00")
    if "+" not in s and "T" in s:
        s = s + "+00:00"
    try:
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except ValueError:
        return 0


def _iso(dt: Any) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    return str(dt)


def _row_to_bindings(r: Sequence[Any], now: str) -> tuple:
    """把 BH 返回行转成 UPSERT 的参数 tuple。"""
    (
        crm_id, name, owner_id, level, industry_id, dim_depart,
        is_customer, is_deleted, recent_activity_at,
        total_order_amount, account_score, state, shared_tags,
        channel, parent_id, created_at, updated_at,
    ) = r
    return (
        crm_id,
        name or "(未命名)",
        owner_id,
        _to_int(level),
        _to_int(industry_id),
        dim_depart,
        1 if is_customer == "1" else 0,
        int(is_deleted) if is_deleted is not None else 0,
        _iso(recent_activity_at),
        _to_float(total_order_amount),
        _to_float(account_score),
        state,
        shared_tags,
        _to_int(channel),
        parent_id,
        _iso(created_at),
        _iso(updated_at) or now,
        now,
    )


EPOCH_DT = datetime(1970, 1, 1)


def _parse_wm(wm: str) -> datetime:
    """解析 ISO 字符串到 naive datetime（CH 的 DateTime64 是 naive，按服务器时区存）。"""
    s = wm.replace("Z", "").split("+")[0]
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return EPOCH_DT


def sync(*, init: bool = False, max_batches: int | None = None) -> dict:
    """执行一次同步。

    返回 {'rows': int, 'batches': int, 'watermark': str}。

    分页用 (updatedAt, id) 复合 cursor 避免同 timestamp 的 tie 漏行。
    """
    scope = "crm_account"
    watermark = sync_state.EPOCH if init else sync_state.get_watermark(scope)
    print(f"[sync_account] start  init={init}  watermark={watermark}")

    ch = bytehouse.client()
    ods = config.BH_DATABASE_ODS

    total = 0
    batches = 0
    max_dt: datetime = _parse_wm(watermark)
    last_id: str = ""

    while True:
        # (updatedAt, id) 复合 cursor：updatedAt > X 或（= X 且 id > last_id）
        rows = ch.execute(
            f"""
            SELECT {','.join(FIELDS)}
            FROM {ods}.crm_account
            WHERE updatedAt IS NOT NULL
              AND (updatedAt > %(wm)s
                   OR (updatedAt = %(wm)s AND id > %(last_id)s))
            ORDER BY updatedAt ASC, id ASC
            LIMIT {BATCH}
            """,
            {"wm": max_dt, "last_id": last_id},
        )

        if not rows:
            break

        now = datetime.now(timezone.utc).isoformat()
        batch_max_dt = max_dt
        batch_last_id = last_id
        conn = connect()
        try:
            with transaction(conn):
                for r in rows:
                    conn.execute(UPSERT, _row_to_bindings(r, now))
                    ua = r[FIELDS.index("updatedAt")]
                    rid = r[FIELDS.index("id")]
                    if ua is not None:
                        # 严格单调（ORDER BY updatedAt ASC, id ASC）
                        batch_max_dt = ua
                        batch_last_id = rid
        finally:
            conn.close()

        max_dt = batch_max_dt
        last_id = batch_last_id
        total += len(rows)
        batches += 1
        print(f"[sync_account] batch {batches}: {len(rows)} rows, watermark → {max_dt} (last_id={last_id})")

        if len(rows) < BATCH:
            break
        if max_batches is not None and batches >= max_batches:
            print(f"[sync_account] stop at max_batches={max_batches}")
            break

    final_wm = max_dt.isoformat()
    sync_state.commit(scope, watermark=final_wm, rows_last=total, ok=True)
    print(f"[sync_account] done  rows={total}  batches={batches}  watermark={final_wm}")
    return {"rows": total, "batches": batches, "watermark": final_wm}


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync ODS_YL.crm_account → local customers")
    parser.add_argument("--init", action="store_true", help="从 epoch 全量拉")
    parser.add_argument("--max-batches", type=int, default=None,
                        help="限制批次数（调试用）")
    args = parser.parse_args()

    try:
        sync(init=args.init, max_batches=args.max_batches)
    except Exception as e:
        sync_state.commit("crm_account",
                          watermark=sync_state.get_watermark("crm_account"),
                          rows_last=0, ok=False, error=repr(e)[:500])
        raise


if __name__ == "__main__":
    main()
