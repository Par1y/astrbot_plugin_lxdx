"""Chunithm (中二节奏) API 客户端：Token 管理、HTTP 请求、自动重试、数据解析。

与 LxnsClient 共享 LxnsAuth 认证实例，API 端点使用 /api/v0/chunithm。
"""

import time
from typing import Callable, Awaitable, Optional

from astrbot.api import logger

from .auth import LxnsAuth
from .rate_limit import GlobalRateLimiter
from .models import (
    TokenInfo,
    ChuSongInfo,
    ChuSongDifficulty,
    ChuScore,
    ChuPlayerBests,
    ChuPlayerInfo,
    ChuSongListResult,
    ChuAlias,
    ChuNotes,
    ChuGenre,
    ChuVersion,
    LxnsError,
    AuthExpiredError,
    AuthRequiredError,
    ApiRequestError,
)

CHUNITHM_BASE = "https://maimai.lxns.net"


class ChunithmClient:
    MAX_RETRIES = 2

    def __init__(
        self,
        auth: LxnsAuth,
        redirect_uri: str = "",
        api_key: str = "",
        is_oauth: bool = True,
        debug: bool = False,
        on_token_refresh: Optional[Callable[[str, TokenInfo], Awaitable[None]]] = None,
        rate_limiter: Optional[GlobalRateLimiter] = None,
    ):
        self._auth = auth
        self._redirect_uri = redirect_uri
        self._api_key = api_key
        self._is_oauth = is_oauth
        self._debug = debug
        self._on_token_refresh = on_token_refresh
        self._rate_limiter = rate_limiter

    async def _throttle(self) -> None:
        """限速等待（每个逻辑请求只等待一次，重试不重复计费）。"""
        if self._rate_limiter:
            await self._rate_limiter.acquire()

    @property
    def _is_api_key_mode(self) -> bool:
        return not self._is_oauth

    @staticmethod
    def _get_httpx():
        try:
            import httpx

            return httpx
        except ImportError:
            raise LxnsError("缺少 httpx 依赖，请安装: pip install httpx")

    @classmethod
    def _http_client(cls):
        h = cls._get_httpx()
        return h.AsyncClient(timeout=h.Timeout(30.0))

    def _endpoint_base(self) -> str:
        return (
            f"{CHUNITHM_BASE}/api/v0/chunithm"
            if self._is_api_key_mode
            else f"{CHUNITHM_BASE}/api/v0/user/chunithm"
        )

    async def _auth_headers(self, uid: str = "") -> dict:
        if self._is_api_key_mode:
            return {"Authorization": self._api_key}
        t = self._auth.get_tokens(uid)
        if not t:
            raise AuthRequiredError("未登录，请使用 /lxchu login 进行 OAuth 授权")
        if LxnsAuth.is_token_expired(t):
            try:
                t = await self._refresh_token(t.refresh_token, uid)
                self._auth.store_tokens(uid, t)
                if self._debug:
                    logger.info(f"[lxdx] Chunithm token refreshed for uid={uid}")
                if callable(self._on_token_refresh):
                    await self._on_token_refresh(uid, t)
            except AuthExpiredError:
                raise
            except LxnsError as e:
                raise AuthExpiredError(f"令牌刷新失败: {e}") from e
        return {"Authorization": f"Bearer {t.access_token}"}

    async def _refresh_token(self, refresh_token: str, uid: str = "") -> TokenInfo:
        h = self._get_httpx()
        pl: dict = {
            "grant_type": "refresh_token",
            "client_id": self._auth.client_id,
            "redirect_uri": self._redirect_uri,
            "refresh_token": refresh_token,
        }
        await self._throttle()
        for i in range(self.MAX_RETRIES + 1):
            try:
                async with self._http_client() as c:
                    r = await c.post(LxnsAuth.TOKEN_URL, json=pl)
                    if r.status_code >= 500 and i < self.MAX_RETRIES:
                        continue
                    if r.status_code in (400, 401):
                        if uid:
                            self._auth.remove_tokens(uid)
                        raise AuthExpiredError("登录已过期，请重新授权 /lxchu login")
                    if r.status_code >= 400:
                        if uid:
                            self._auth.remove_tokens(uid)
                        raise AuthExpiredError(f"刷新令牌失败: [{r.status_code}]")
                    d = r.json()
                    td = d.get("data", d)
                    return LxnsAuth.make_token_info(
                        td["access_token"],
                        td["refresh_token"],
                        td.get("expires_in", 900),
                    )
            except (h.TimeoutException, h.ConnectError) as e:
                if i < self.MAX_RETRIES:
                    continue
                raise LxnsError(f"连接落雪服务器超时: {e}") from e
            except LxnsError:
                raise
        raise LxnsError("刷新令牌失败")

    async def _api_get(self, url: str, uid: str = "", auth: bool = True) -> dict:
        """通用 GET 请求：auth=False 时不附加认证头（用于公共 API）。"""
        h = self._get_httpx()
        hdrs = await self._auth_headers(uid) if auth else {}
        await self._throttle()
        for i in range(self.MAX_RETRIES + 1):
            try:
                t0 = time.monotonic()
                async with self._http_client() as c:
                    r = await c.get(url, headers=hdrs)
                    elapsed = time.monotonic() - t0
                    if self._debug:
                        logger.info(
                            f"[lxdx] GET {url} -> {r.status_code} ({elapsed:.2f}s)"
                        )
                    if auth and r.status_code == 401:
                        raise AuthExpiredError("登录已过期，请重新授权 /lxchu login")
                    if r.status_code >= 500 and i < self.MAX_RETRIES:
                        continue
                    r.raise_for_status()
                    return r.json()
            except (h.TimeoutException, h.ConnectError) as e:
                if self._debug:
                    logger.info(f"[lxdx] GET {url} -> timeout/connect error: {e}")
                if i < self.MAX_RETRIES:
                    continue
                raise ApiRequestError(f"请求超时: {e}") from e
            except LxnsError:
                raise
            except h.HTTPStatusError as e:
                raise ApiRequestError(f"API 请求失败 [{e.response.status_code}]") from e
        raise ApiRequestError("请求失败")

    # --- Player APIs ---

    async def get_player_info(self, fc: int = 0, uid: str = "") -> ChuPlayerInfo:
        b = self._endpoint_base()
        if self._is_api_key_mode and fc:
            u = f"{b}/player/{fc}"
        elif self._is_api_key_mode:
            raise ApiRequestError("开发者 API 模式需要提供 friend_code")
        else:
            u = f"{b}/player"
        d = await self._api_get(u, uid)
        return self._parse_player(d.get("data", d))

    async def get_bests(self, fc: int = 0, uid: str = "") -> ChuPlayerBests:
        b = self._endpoint_base()
        if self._is_api_key_mode and fc:
            u = f"{b}/player/{fc}/bests"
        elif self._is_api_key_mode:
            raise ApiRequestError("开发者 API 模式需要提供 friend_code")
        else:
            u = f"{b}/player/bests"
        d = await self._api_get(u, uid)
        return self._parse_bests(d.get("data", d) if "data" in d else d)

    async def get_recents(self, fc: int = 0, uid: str = "") -> list[ChuScore]:
        b = self._endpoint_base()
        if self._is_api_key_mode and fc:
            u = f"{b}/player/{fc}/recents"
        elif self._is_api_key_mode:
            raise ApiRequestError("开发者 API 模式需要提供 friend_code")
        else:
            u = f"{b}/player/recents"
        d = await self._api_get(u, uid)
        arr = d.get("data", d)
        if isinstance(arr, dict):
            arr = arr.get("scores", arr.get("recents", []))
        return [self._parse_score(i) for i in (arr if isinstance(arr, list) else [])]

    async def get_scores(self, fc: int = 0, uid: str = "") -> list[ChuScore]:
        b = self._endpoint_base()
        if self._is_api_key_mode and fc:
            u = f"{b}/player/{fc}/scores"
        elif self._is_api_key_mode:
            raise ApiRequestError("开发者 API 模式需要提供 friend_code")
        else:
            u = f"{b}/player/scores"
        d = await self._api_get(u, uid)
        arr = d.get("data", d)
        return [self._parse_score(i) for i in (arr if isinstance(arr, list) else [])]

    async def get_player_best(
        self,
        song_id: int,
        level_index: int = 0,
        fc: int = 0,
        uid: str = "",
    ) -> Optional[ChuScore]:
        """获取玩家在特定谱面的最佳成绩。

        Args:
            song_id: 曲目ID
            level_index: 难度索引（0-5）
            fc: 好友码（API Key模式）
            uid: 用户ID（OAuth模式）
        """
        b = self._endpoint_base()
        params = [f"song_id={song_id}"]

        if level_index >= 0:
            params.append(f"level_index={level_index}")

        query = "&".join(params)

        if self._is_api_key_mode and fc:
            url = f"{b}/player/{fc}/best?{query}"
        elif self._is_api_key_mode:
            raise ApiRequestError("开发者 API 模式需要提供 friend_code")
        else:
            url = f"{b}/player/best?{query}"

        d = await self._api_get(url, uid)
        # 提取data字段
        inner = d.get("data", d)
        return self._parse_score(inner) if inner and inner.get("id") else None

    # --- Song APIs ---

    async def get_song_list(self, uid: str = "") -> ChuSongListResult:
        d = await self._api_get(
            f"{CHUNITHM_BASE}/api/v0/chunithm/song/list", auth=False
        )
        inner = d.get("data", d)
        songs = [self._parse_song(s) for s in inner.get("songs", [])]
        genres = [
            ChuGenre(id=g.get("id", 0), genre=g.get("genre", ""))
            for g in inner.get("genres", [])
        ]
        versions = [
            ChuVersion(
                id=v.get("id", 0), title=v.get("title", ""), version=v.get("version", 0)
            )
            for v in inner.get("versions", [])
        ]
        return ChuSongListResult(songs=songs, genres=genres, versions=versions)

    async def get_alias_list(self) -> dict[int, list[str]]:
        """获取别名列表。GET /api/v0/chunithm/alias/list"""
        d = await self._api_get(
            f"{CHUNITHM_BASE}/api/v0/chunithm/alias/list", auth=False
        )
        result: dict[int, list[str]] = {}
        for item in d.get("aliases", []):
            sid = item.get("song_id", 0)
            aliases = item.get("aliases", [])
            if sid and aliases:
                result[sid] = aliases
        return result

    async def get_song(self, song_id: int, uid: str = "") -> Optional[ChuSongInfo]:
        d = await self._api_get(
            f"{CHUNITHM_BASE}/api/v0/chunithm/song/{song_id}", auth=False
        )
        inner = d.get("data", d)
        if not inner or not inner.get("id"):
            return None
        return self._parse_song(inner)

    async def close(self) -> None:
        pass

    # --- 响应解析器 ---

    @staticmethod
    def _parse_player(item: dict) -> ChuPlayerInfo:
        return ChuPlayerInfo(
            name=item.get("name", ""),
            level=item.get("level", 0),
            rating=item.get("rating", 0.0),
            rating_possession=item.get("rating_possession", ""),
            friend_code=item.get("friend_code", 0),
            class_emblem=item.get("class_emblem", {"base": 0, "medal": 0}),
            reborn_count=item.get("reborn_count", 0),
            over_power=item.get("over_power", 0.0),
            over_power_progress=item.get("over_power_progress", 0.0),
            currency=item.get("currency", 0),
            total_currency=item.get("total_currency", 0),
            total_play_count=item.get("total_play_count", 0),
            trophy=item.get("trophy"),
            character=item.get("character"),
            name_plate=item.get("name_plate"),
            map_icon=item.get("map_icon"),
            upload_time=item.get("upload_time"),
        )

    @staticmethod
    def _parse_score(item: dict) -> ChuScore:
        return ChuScore(
            id=item.get("id", 0),
            score=item.get("score", 0),
            rating=item.get("rating", 0.0),
            over_power=item.get("over_power", 0.0),
            level_index=item.get("level_index", 0),
            song_name=item.get("song_name", ""),
            level=item.get("level", ""),
            clear=item.get("clear", ""),
            full_combo=item.get("full_combo", ""),
            full_chain=item.get("full_chain", ""),
            rank=item.get("rank", ""),
            play_time=item.get("play_time"),
            upload_time=item.get("upload_time"),
            last_played_time=item.get("last_played_time"),
        )

    @classmethod
    def _parse_bests(cls, inner: dict) -> ChuPlayerBests:
        return ChuPlayerBests(
            bests=[cls._parse_score(i) for i in inner.get("bests", [])],
            selections=[cls._parse_score(i) for i in inner.get("selections", [])],
            new_bests=[cls._parse_score(i) for i in inner.get("new_bests", [])],
        )

    @staticmethod
    def _parse_notes(item: dict) -> ChuNotes:
        return ChuNotes(
            total=item.get("total", 0),
            tap=item.get("tap", 0),
            hold=item.get("hold", 0),
            slide=item.get("slide", 0),
            air=item.get("air", 0),
            flick=item.get("flick", 0),
        )

    @classmethod
    def _parse_difficulty(cls, item: dict) -> ChuSongDifficulty:
        notes = None
        if item.get("notes"):
            notes = cls._parse_notes(item["notes"])
        return ChuSongDifficulty(
            difficulty=item.get("difficulty", 0),
            level=item.get("level", ""),
            level_value=item.get("level_value", 0.0),
            note_designer=item.get("note_designer", ""),
            version=item.get("version", 0),
            notes=notes,
            origin_id=item.get("origin_id", 0),
            kanji=item.get("kanji", ""),
            star=item.get("star", 0),
        )

    @classmethod
    def _parse_song(cls, item: dict) -> ChuSongInfo:
        diffs = [cls._parse_difficulty(d) for d in item.get("difficulties", [])]
        return ChuSongInfo(
            id=item.get("id", 0),
            title=item.get("title", ""),
            artist=item.get("artist", ""),
            genre=item.get("genre", ""),
            bpm=item.get("bpm", 0),
            version=item.get("version", 0),
            difficulties=diffs,
            map=item.get("map", ""),
            rights=item.get("rights", ""),
            locked=item.get("locked", False),
            disabled=item.get("disabled", False),
        )
