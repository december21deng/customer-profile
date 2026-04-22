#!/usr/bin/env bash
# Fly.io entrypoint：同时跑 H5 (uvicorn) 和 CRM Scheduler。
# 任意一个挂了就整个容器退出，让 fly 重启（状态靠 /data volume 持久化）。
set -euo pipefail

echo "[start] migrating schema..."
python -m src.db.migrate

echo "[start] launching scheduler (background)..."
python -m src.crm.scheduler &
SCHED_PID=$!

echo "[start] launching H5 on 0.0.0.0:8080..."
uvicorn src.web.app:app --host 0.0.0.0 --port 8080 &
WEB_PID=$!

# 转发 SIGTERM/SIGINT 给两个子进程
trap 'echo "[start] shutting down..."; kill -TERM $SCHED_PID $WEB_PID 2>/dev/null || true' SIGTERM SIGINT

# 任一子进程退出就整体退出
wait -n
echo "[start] a child exited, killing the rest"
kill -TERM $SCHED_PID $WEB_PID 2>/dev/null || true
wait
exit 1
