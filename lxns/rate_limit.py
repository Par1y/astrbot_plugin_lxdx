"""全局 API 请求间隔限制：所有落雪 API 请求之间保持可配置的最小间隔（串行排队）。

多个命令并发触发请求时按到达顺序排队，每个请求至少间隔 interval 秒，
防止群成员刷命令耗尽宿主的 API 配额。interval <= 0 时完全不限速。
"""

import asyncio
import time


class GlobalRateLimiter:
    """全局最小间隔限速器（所有调用方共享一个实例）。"""

    def __init__(self, interval: float = 0.0):
        self._interval = max(0.0, float(interval))
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def acquire(self) -> None:
        """排队等待下一个可用请求槽位。interval <= 0 时直接放行。"""
        if self._interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_at = now + self._interval
