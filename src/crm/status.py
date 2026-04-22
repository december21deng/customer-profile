"""`python -m src.crm.status` - 查看本地镜像状态。"""

from __future__ import annotations

from src.crm import bytehouse
from src import config
from src.db.connection import connect


def show() -> None:
    conn = connect()
    try:
        print("=" * 70)
        print("LOCAL (SQLite)")
        print("=" * 70)

        # 表行数
        tables = ["customers", "crm_users", "crm_value_list", "sync_state"]
        for t in tables:
            n = conn.execute(f"SELECT count() AS c FROM {t}").fetchone()["c"]
            print(f"  {t:20s} {n:>10,} rows")

        # 客户细分
        row = conn.execute(
            """
            SELECT
                sum(crm_is_deleted=0)               AS alive,
                sum(crm_is_deleted=1)               AS deleted,
                sum(crm_is_customer=1 AND crm_is_deleted=0) AS customers,
                sum(crm_is_customer=0 AND crm_is_deleted=0) AS leads,
                max(crm_updated_at)                 AS latest_updated
            FROM customers
            """
        ).fetchone()
        print("\n  customers breakdown:")
        print(f"    alive      {row['alive'] or 0:>10,}")
        print(f"    deleted    {row['deleted'] or 0:>10,}")
        print(f"    customers  {row['customers'] or 0:>10,}  (isCustomer=1)")
        print(f"    leads      {row['leads'] or 0:>10,}  (isCustomer=0)")
        print(f"    latest updatedAt: {row['latest_updated']}")

        # sync_state
        print("\n  sync_state:")
        for r in conn.execute(
            "SELECT scope, watermark, last_run_at, last_run_ok, rows_total, rows_last, last_error "
            "FROM sync_state ORDER BY scope"
        ):
            flag = "OK " if r["last_run_ok"] else "ERR"
            print(f"    [{flag}] {r['scope']:18s}  wm={r['watermark']}  "
                  f"run={r['last_run_at']}  total={r['rows_total']}  last={r['rows_last']}")
            if r["last_error"]:
                print(f"          error: {r['last_error']}")

        # Top owners
        print("\n  top 5 owners (by active customer count):")
        for r in conn.execute(
            """
            SELECT crm_owner_id, u.display_name, count() AS n
            FROM customers c LEFT JOIN crm_users u ON u.id = c.crm_owner_id
            WHERE crm_is_deleted=0 AND crm_owner_id IS NOT NULL
            GROUP BY crm_owner_id, u.display_name
            ORDER BY n DESC LIMIT 5
            """
        ):
            name = r["display_name"] or "(未知)"
            print(f"    {r['crm_owner_id']:20s} {name:20s} {r['n']:>6,}")
    finally:
        conn.close()

    # 远端对比
    if config.BH_PASSWORD:
        print()
        print("=" * 70)
        print("REMOTE (ByteHouse)")
        print("=" * 70)
        ch = bytehouse.client()
        remote_count, remote_max = ch.execute(
            f"SELECT count(), max(updatedAt) FROM {config.BH_DATABASE_ODS}.crm_account "
            "WHERE is_crm_deleted=0"
        )[0]
        print(f"  ODS_YL.crm_account alive rows:    {remote_count:>10,}")
        print(f"  max(updatedAt):                   {remote_max}")


if __name__ == "__main__":
    show()
