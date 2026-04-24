"""Minute / docx 标题的内存 TTL 缓存。

用户说文档标题会改，所以不存 DB（会陈旧）。每次渲染详情页都实时拉，
但同一个 token 在 5 分钟内共享结果，避免刷新页面都打飞书 API。

单进程内存缓存；Fly 目前单实例，OK。如以后多实例，可以换 sqlite 或 Redis。
"""
from __future__ import annotations

import time
import threading

_TTL_SECONDS = 300  # 5 分钟
_MAX_ENTRIES = 1000  # 防止无限涨

_cache: dict[tuple[str, str], tuple[str | None, float]] = {}
_lock = threading.Lock()


def get(kind: str, token: str) -> tuple[bool, str | None]:
    """返回 (是否命中, 缓存值)。未命中返回 (False, None)。
    命中时即使值是 None（拉取失败），也会返回 (True, None)，避免重复拉。"""
    if not token:
        return False, None
    now = time.time()
    with _lock:
        entry = _cache.get((kind, token))
        if entry is None:
            return False, None
        value, expires_at = entry
        if expires_at <= now:
            _cache.pop((kind, token), None)
            return False, None
        return True, value


def put(kind: str, token: str, value: str | None) -> None:
    """缓存结果（value 可以是 None，用于记"这次拉失败了，下次别立刻再拉"）。"""
    if not token:
        return
    with _lock:
        if len(_cache) >= _MAX_ENTRIES:
            # 简单 eviction：删过期的；还不够就随便丢一个
            now = time.time()
            expired = [k for k, (_, exp) in _cache.items() if exp <= now]
            for k in expired:
                _cache.pop(k, None)
            if len(_cache) >= _MAX_ENTRIES:
                _cache.pop(next(iter(_cache)))
        _cache[(kind, token)] = (value, time.time() + _TTL_SECONDS)
