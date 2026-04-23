"""并发控制：每客户一把 asyncio.Lock + 全局并发上限 Semaphore。

只对 **同一进程** 的并发生效。Fly 当前 `min_machines_running=1`，
多进程/多机部署前这够用。

用法：
    async with acquire(customer_id):
        ...

设计要点：
- 同一 customer_id 的多次 ingest 串行（后来的等前面的）
- 不同 customer_id 可以并发，最多 GLOBAL_CONCURRENCY 条
- Lock 对象按需 lazy 创建，永不回收（客户数级别，内存可忽略）
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

# 同一进程最多同时跑多少个 ingest（Anthropic TPM/RPM + CPU/内存考虑）
GLOBAL_CONCURRENCY = 3

_global_sem = asyncio.Semaphore(GLOBAL_CONCURRENCY)
_locks: dict[str, asyncio.Lock] = {}
_locks_mu = asyncio.Lock()  # 保护 _locks 本身的写入


async def _get_lock(customer_id: str) -> asyncio.Lock:
    # 单写 _locks（读可以无锁）
    async with _locks_mu:
        lock = _locks.get(customer_id)
        if lock is None:
            lock = asyncio.Lock()
            _locks[customer_id] = lock
        return lock


@asynccontextmanager
async def acquire(customer_id: str):
    """嵌套顺序：全局 Semaphore → 客户 Lock。"""
    async with _global_sem:
        lock = await _get_lock(customer_id)
        async with lock:
            yield
