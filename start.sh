#!/usr/bin/env bash
# Fly.io entrypoint：同时跑 H5 (uvicorn) 和 CRM Scheduler。
# 任意一个挂了就整个容器退出，让 fly 重启（状态靠 /data volume 持久化）。
set -euo pipefail

# ---- /data 持久化初始化（仅 Fly 环境有 /data；本地 docker 跑没有则跳过）----
if [ -d /data ]; then
    echo "[start] preparing /data volume layout..."
    mkdir -p /data/raw/customers /data/wiki/customers

    # /data/.git：wiki/raw 版本化仓库（独立于代码仓，不推远端）
    if [ ! -d /data/.git ]; then
        echo "[start] initializing /data/.git ..."
        git -C /data init -q
        git -C /data config user.email "bot@customer-profile-dec.fly.dev"
        git -C /data config user.name  "ingest-bot"
        git -C /data commit --allow-empty -m "init" -q
    fi

    # symlink：让 agent SDK 的 cwd=/app 下能访问 wiki/ raw/
    # -sfn：symbolic + force + no-dereference（避免把已有 symlink 当目录穿进去）
    ln -sfn /data/raw  /app/raw
    ln -sfn /data/wiki /app/wiki
    echo "[start] /app/wiki -> $(readlink /app/wiki)"
    echo "[start] /app/raw  -> $(readlink /app/raw)"
fi

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
