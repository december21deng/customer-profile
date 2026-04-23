"""一次性：给老的 followup_records 回填 meeting_title / progress_line。

用法：
    python -m src.scripts.backfill_extract           # 只补 meeting_title 为空的
    python -m src.scripts.backfill_extract --all     # 重算所有记录（覆盖）
    python -m src.scripts.backfill_extract --dry     # 打印要处理的记录，不实际跑 AI

Fly 上跑：
    flyctl ssh console -C "python -m src.scripts.backfill_extract"

逻辑：
    1. 扫 followup_records，按 meeting_date + customer_id 匹配 raw/customers/ 下的文件
    2. 读 wiki/customers/{customer_id}.md
    3. 调 run_extract（沿用现有 prompt + tools，返 dict）
    4. UPDATE 两个字段（不改 summary，避免覆盖用户已看过的文本）
    5. 失败记录下来，最后打印统计
"""
from __future__ import annotations

import argparse
import logging
import sys

from src.config import PROJECT_ROOT
from src.db.connection import connect
from src.ingest.pipeline import (
    RAW_DIR, WIKI_DIR,
    _clean_progress, _clean_title, run_extract,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill")


def _find_raw(customer_id: str, meeting_date: str):
    """按命名约定 {date}-{customer_id}-{hash}.md 找 raw 文件。"""
    pattern = f"{meeting_date[:10]}-{customer_id}-*.md"
    candidates = sorted((RAW_DIR / "customers").glob(pattern))
    # 排除 .extract.json 的兄弟文件
    candidates = [p for p in candidates if p.suffix == ".md" and ".extract" not in p.name]
    return candidates[0] if candidates else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="重跑所有记录（默认只补空的）")
    ap.add_argument("--dry", action="store_true",
                    help="只打印要处理的记录，不实际调 AI")
    args = ap.parse_args()

    conn = connect()
    try:
        if args.all:
            rows = conn.execute(
                "SELECT id, customer_id, meeting_date, meeting_title, progress_line "
                "FROM followup_records ORDER BY meeting_date DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, customer_id, meeting_date, meeting_title, progress_line "
                "FROM followup_records "
                "WHERE COALESCE(meeting_title,'') = '' "
                "   OR COALESCE(progress_line,'') = '' "
                "ORDER BY meeting_date DESC"
            ).fetchall()
    finally:
        conn.close()

    if not rows:
        log.info("nothing to backfill, all records already have meeting_title + progress_line")
        return 0

    log.info("candidates: %d record(s)", len(rows))

    ok, skipped, failed = 0, 0, 0
    for r in rows:
        rid, cid, md = r["id"], r["customer_id"], r["meeting_date"]
        raw_path = _find_raw(cid, md)
        if raw_path is None or not raw_path.exists():
            log.warning("skip %s (%s %s): raw file not found", rid, cid, md[:10])
            skipped += 1
            continue

        wiki_path = WIKI_DIR / "customers" / f"{cid}.md"
        wiki_text = wiki_path.read_text(encoding="utf-8") if wiki_path.exists() else ""
        raw_text = raw_path.read_text(encoding="utf-8")

        if args.dry:
            log.info("DRY %s  raw=%s  wiki=%s",
                     rid, raw_path.relative_to(PROJECT_ROOT),
                     "yes" if wiki_text else "no")
            continue

        try:
            result = run_extract(wiki_text, raw_text, log_prefix=f"backfill:{rid[:8]}")
        except Exception as e:
            log.exception("failed extract for %s: %s", rid, e)
            failed += 1
            continue

        title = _clean_title(result.get("meeting_title") or "")
        progress = _clean_progress(result.get("progress_line") or "")
        if not title and not progress:
            log.warning("%s: extract returned both empty, skipping write", rid)
            skipped += 1
            continue

        conn = connect()
        try:
            conn.execute(
                "UPDATE followup_records "
                "SET meeting_title = ?, progress_line = ? "
                "WHERE id = ?",
                (title, progress, rid),
            )
            conn.commit()
        finally:
            conn.close()

        log.info("ok %s: title=%r progress=%r", rid, title, progress)
        ok += 1

    log.info("done  ok=%d skipped=%d failed=%d", ok, skipped, failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
