"""插件存储管理：文件目录路径和 KV key 命名。KV 操作直接委托给 Star 实例的 put_kv_data / get_kv_data / delete_kv_data。

get_kv_data(key, False) — 第二个参数是布尔值，非默认回退值。

KV 为数据库持久存储；本插件写入的 key 会记入持久化索引（KV_INDEX_KEY，同为一条 KV 记录），
保证 kv_clear_all 在插件重启后仍能清点并删除全部历史数据（token / binding 等）。
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Any

from astrbot.api import logger

if TYPE_CHECKING:
    from astrbot.api.star import Star


class StorageManager:
    """管理文件目录路径、确保目录存在、以及 KV 操作的 key 命名与清点。"""

    KV_INDEX_KEY = "lxdx_kv_index"

    def __init__(self, plugin: Star, data_dir: str, debug: bool = False):
        self._plugin = plugin
        self._debug = debug
        self._keys: set[str] = set()
        self._index_loaded = False
        self._ilock = asyncio.Lock()
        self.assets_dir = f"{data_dir}/plugin_data/astrbot_plugin_lxdx/assets"
        self.cache_dir = f"{data_dir}/plugin_data/astrbot_plugin_lxdx/cache"

    def ensure_dirs(self) -> None:
        """确保 assets 和 cache 目录存在。"""
        os.makedirs(self.assets_dir, exist_ok=True)
        os.makedirs(self.cache_dir, exist_ok=True)

    # --- KV key 索引（持久化，供 kv_clear_all 跨重启清点） ---

    async def _load_index(self) -> None:
        if self._index_loaded:
            return
        try:
            data = await self._plugin.get_kv_data(self.KV_INDEX_KEY, False)
        except Exception as e:
            logger.warning(f"[lxdx] KV index load failed: {e}")
            data = None
        if isinstance(data, list):
            self._keys = {k for k in data if isinstance(k, str)}
        self._index_loaded = True

    async def _save_index(self) -> None:
        try:
            await self._plugin.put_kv_data(self.KV_INDEX_KEY, sorted(self._keys))
        except Exception as e:
            logger.warning(f"[lxdx] KV index save failed: {e}")

    async def kv_put(self, key: str, value: Any) -> None:
        if self._debug:
            logger.info(f"[lxdx] KV put {key}")
        await self._plugin.put_kv_data(key, value)
        async with self._ilock:
            await self._load_index()
            self._keys.add(key)
            await self._save_index()

    async def kv_get(self, key: str) -> Any:
        try:
            return await self._plugin.get_kv_data(key, False)
        except Exception:
            return None

    async def kv_delete(self, key: str) -> None:
        await self._plugin.delete_kv_data(key)
        async with self._ilock:
            await self._load_index()
            if key in self._keys:
                self._keys.discard(key)
                await self._save_index()

    async def kv_clear_all(self) -> None:
        """删除本插件写入的全部 KV 数据（含索引自身）。索引持久化，跨重启有效。"""
        async with self._ilock:
            await self._load_index()
            if self._debug:
                logger.info(f"[lxdx] KV clear all ({len(self._keys)} keys)")
            for key in list(self._keys):
                try:
                    await self._plugin.delete_kv_data(key)
                except Exception as e:
                    logger.warning(f"[lxdx] KV delete {key} failed: {e}")
            self._keys.clear()
            try:
                await self._plugin.delete_kv_data(self.KV_INDEX_KEY)
            except Exception as e:
                logger.warning(f"[lxdx] KV index delete failed: {e}")

    def binding_key(self, uid: str) -> str:
        return f"binding:{uid}"

    def chu_binding_key(self, uid: str) -> str:
        return f"chubind:{uid}"

    def token_key(self, uid: str) -> str:
        return f"token:{uid}"
