"""CRM 同步调度器。

用法：
    python -m src.crm.scheduler           # 前台常驻，每 CRM_SYNC_INTERVAL 秒跑一轮
    python -m src.crm.scheduler --once    # 跑一次就退出

一轮 = valuelist → users → account → stages 依次跑。单进程串行，无并发。
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from datetime import datetime, timezone

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from src.crm import sync_account, sync_stages, sync_users, sync_valuelist

INTERVAL = int(os.environ.get("CRM_SYNC_INTERVAL", str(15 * 60)))  # 默认 15 分钟


def sync_all() -> None:
    """跑一轮。出错不让整轮挂，记录后继续下一个 step。"""
    t0 = time.time()
    print(f"[sync-all] start @ {datetime.now(timezone.utc).isoformat()}")
    for name, fn in [
        ("valuelist", sync_valuelist.sync),
        ("users",     sync_users.sync),
        ("account",   sync_account.sync),
        ("stages",    sync_stages.sync),
    ]:
        try:
            res = fn()
            print(f"[sync-all]   {name}: {res}")
        except Exception as e:
            print(f"[sync-all]   {name} FAIL: {e!r}", file=sys.stderr)
    print(f"[sync-all] done  {time.time() - t0:.1f}s")


def main() -> None:
    parser = argparse.ArgumentParser(description="CRM sync scheduler")
    parser.add_argument("--once", action="store_true", help="跑一次就退出")
    args = parser.parse_args()

    if args.once:
        sync_all()
        return

    sync_all()  # 启动先跑一轮

    sched = BlockingScheduler(timezone="UTC")
    sched.add_job(sync_all, IntervalTrigger(seconds=INTERVAL),
                  id="sync-all", max_instances=1, coalesce=True)
    print(f"[scheduler] running. interval={INTERVAL}s")

    def _stop(signum, frame):
        print(f"[scheduler] got signal {signum}, shutting down")
        sched.shutdown(wait=False)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    sched.start()


if __name__ == "__main__":
    main()
