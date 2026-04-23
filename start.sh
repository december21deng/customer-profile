#!/usr/bin/env bash
# Fly.io entrypoint：同时跑 H5 (uvicorn) 和 CRM Scheduler。
# 任意一个挂了就整个容器退出，让 fly 重启（状态靠 /data volume 持久化）。
set -euo pipefail

# ---- 以 root 跑 /data 初始化（volume 默认 root 所有），再切到 app 用户 ----
# Claude CLI 的 --dangerously-skip-permissions 在 root 下会被拒绝，所以必须切。
if [ "$(id -u)" = "0" ]; then
    if [ -d /data ]; then
        echo "[start] preparing /data volume layout (as root)..."
        mkdir -p /data/raw/customers /data/wiki/customers

        if [ ! -d /data/.git ]; then
            echo "[start] initializing /data/.git ..."
            git -C /data init -q
            git -C /data config user.email "bot@customer-profile-dec.fly.dev"
            git -C /data config user.name  "ingest-bot"
            git -C /data commit --allow-empty -m "init" -q
        fi

        ln -sfn /data/raw  /app/raw
        ln -sfn /data/wiki /app/wiki
        echo "[start] /app/wiki -> $(readlink /app/wiki)"
        echo "[start] /app/raw  -> $(readlink /app/raw)"

        # /data volume 默认 root 所有，让 app 用户能读写
        chown -R app:app /data
    fi

    echo "[start] dropping root, re-exec as user 'app' via gosu..."
    exec gosu app /app/start.sh "$@"
fi

# --- 下面这段以 app 用户身份运行 ---
echo "[start] running as $(id -un) ($(id -u))"

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
