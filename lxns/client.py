"""LXNS API 客户端：Token 管理、HTTP 请求、自动重试、数据解析。httpx 懒加载。"""

from typing import Optional, Callable, Awaitable
import time

from astrbot.api import logger

from .auth import LxnsAuth
from .models import (
    PlayerB50,
    PlayerRecord,
    PlayerInfo,
    SongInfo,
    SongRecord,
    Score,
    TokenInfo,
    UserProfile,
    LxnsError,
    AuthExpiredError,
    AuthRequiredError,
    ApiRequestError,
)

BASE_URL = "https://maimai.lxns.net"


class LxnsClient:
    """LXNS API 客户端，支持 OAuth (PKCE) 和开发者 API Key。

    核心设计：
    - 每次请求创建新的 httpx.AsyncClient（简化生命周期管理）
    - 所有 API 请求最多重试 MAX_RETRIES 次（5xx/超时）
    - OAuth Token 过期时自动刷新（_auth_headers 中触发）
    """

    MAX_RETRIES = 2

    def __init__(
        self,
        auth: LxnsAuth,
        redirect_uri: str = "",
        api_key: str = "",
        is_oauth: bool = True,
        debug: bool = False,
        on_token_refresh: Optional[Callable[[str, TokenInfo], Awaitable[None]]] = None,
    ):
        """auth: PKCE 授权管理实例；redirect_uri: OAuth 回调地址；api_key: 开发者 Key（可选）；
        is_oauth: 是否为OAuth模式（True=OAuth, False=API Key）；
        debug: 开启调试日志；on_token_refresh: Token 刷新时将新 Token 持久化到 KV 的回调（uid, TokenInfo）→ awaitable。"""
        self._auth = auth
        self._redirect_uri = redirect_uri
        self._api_key = api_key
        self._is_oauth = is_oauth
        self._debug = debug
        self._on_token_refresh = on_token_refresh

    @staticmethod
    def _get_httpx():
        """懒加载 httpx 模块。未安装时抛出 LxnsError，避免 import 时报错。"""
        try:
            import httpx

            return httpx
        except ImportError:
            raise LxnsError("缺少 httpx 依赖，请安装: pip install httpx")

    @classmethod
    def _http_client(cls):
        """创建新的 AsyncClient（超时 30s）。每次请求独立创建以简化生命周期。"""
        h = cls._get_httpx()
        return h.AsyncClient(timeout=h.Timeout(30.0))

    def _token_payload(self, grant_type: str, **extra) -> dict:
        """构建 OAuth Token 请求体，包含 grant_type / client_id / redirect_uri 基础字段。"""
        p: dict = {
            "grant_type": grant_type,
            "client_id": self._auth.client_id,
            "redirect_uri": self._redirect_uri,
        }
        p.update(extra)
        return p

    @staticmethod
    def _extract_token_data(resp: dict) -> dict:
        """从 Token 接口响应中提取 data 字段，校验必要字段存在。"""
        d = resp.get("data", resp)
        if not isinstance(d, dict):
            raise LxnsError("服务器返回了异常的数据格式")
        for k in ("access_token", "refresh_token"):
            if k not in d:
                raise LxnsError(f"服务器响应缺少必要字段: {k}")
        return d

    @staticmethod
    def _http_error_msg(code: int, body) -> str:
        """将 HTTP 状态码和响应体格式化为人类可读的错误信息。"""
        if isinstance(body, dict):
            m = body.get("message", body.get("error", ""))
            if m:
                return f"[{code}] {m}"
        return f"HTTP {code}"

    async def exchange_code(self, code: str, code_verifier: str) -> TokenInfo:
        """用 OAuth 授权码交换 Token（PKCE 流程第三步）。支持 5xx 和网络超时重试。"""
        h = self._get_httpx()
        pl = self._token_payload(
            "authorization_code", code=code, code_verifier=code_verifier
        )
        for i in range(self.MAX_RETRIES + 1):
            try:
                async with self._http_client() as c:
                    r = await c.post(LxnsAuth.TOKEN_URL, json=pl)
                    if r.status_code >= 500 and i < self.MAX_RETRIES:
                        continue
                    if r.status_code >= 400:
                        try:
                            b = r.json()
                        except Exception:
                            b = r.text
                        raise LxnsError(
                            f"授权码交换失败: {self._http_error_msg(r.status_code, b)}"
                        )
                    d = self._extract_token_data(r.json())
                    return LxnsAuth.make_token_info(
                        d["access_token"], d["refresh_token"], d.get("expires_in", 900)
                    )
            except (h.TimeoutException, h.ConnectError) as e:
                if i < self.MAX_RETRIES:
                    continue
                raise LxnsError(f"连接落雪服务器超时: {e}") from e
            except LxnsError:
                raise
        raise LxnsError("连接落雪服务器失败")

    async def refresh_token(self, refresh_token: str, uid: str = "") -> TokenInfo:
        """用 refresh_token 刷新 OAuth Token。400/401 时清除缓存并抛 AuthExpiredError。"""
        h = self._get_httpx()
        pl = self._token_payload("refresh_token", refresh_token=refresh_token)
        for i in range(self.MAX_RETRIES + 1):
            try:
                async with self._http_client() as c:
                    r = await c.post(LxnsAuth.TOKEN_URL, json=pl)
                    if r.status_code >= 500 and i < self.MAX_RETRIES:
                        continue
                    if r.status_code in (400, 401):
                        if uid:
                            self._auth.remove_tokens(uid)
                        raise AuthExpiredError("登录已过期，请重新授权 /lxdx login")
                    if r.status_code >= 400:
                        try:
                            b = r.json()
                        except Exception:
                            b = r.text
                        if uid:
                            self._auth.remove_tokens(uid)
                        raise AuthExpiredError(
                            f"刷新令牌失败: {self._http_error_msg(r.status_code, b)}"
                        )
                    d = self._extract_token_data(r.json())
                    return LxnsAuth.make_token_info(
                        d["access_token"], d["refresh_token"], d.get("expires_in", 900)
                    )
            except (h.TimeoutException, h.ConnectError) as e:
                if i < self.MAX_RETRIES:
                    continue
                raise LxnsError(f"连接落雪服务器超时: {e}") from e
            except LxnsError:
                raise
        raise LxnsError("连接落雪服务器失败")

    async def _auth_headers(self, uid: str = "") -> dict:
        """构建认证请求头。API Key 模式返回 Authorization；OAuth 模式返回 Bearer Token，过期时自动刷新。"""
        if not self._is_oauth:
            return {"Authorization": self._api_key}
        t = self._auth.get_tokens(uid)
        if not t:
            raise AuthRequiredError("未登录，请使用 /lxdx login 进行 OAuth 授权")
        if LxnsAuth.is_token_expired(t):
            try:
                t = await self.refresh_token(t.refresh_token, uid)
                self._auth.store_tokens(uid, t)
                if self._debug:
                    logger.info(f"[lxdx] token refreshed for uid={uid}")
                if callable(self._on_token_refresh):
                    await self._on_token_refresh(uid, t)
            except AuthExpiredError:
                raise
            except LxnsError as e:
                raise AuthExpiredError(f"令牌刷新失败: {e}") from e
        return {"Authorization": f"Bearer {t.access_token}"}

    async def _api_get(self, url: str, uid: str = "", auth: bool = True) -> dict:
        """通用 GET 请求：auth=False 时不附加认证头（用于公共 API）。"""
        h = self._get_httpx()
        hdrs = await self._auth_headers(uid) if auth else {}
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
                        raise AuthExpiredError("登录已过期，请重新授权 /lxdx login")
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

    def _endpoint_base(self) -> str:
        """根据认证模式选择 API 基础路径：API Key 用 /maimai，OAuth 用 /user/maimai。"""
        return (
            f"{BASE_URL}/api/v0/maimai"
            if not self._is_oauth
            else f"{BASE_URL}/api/v0/user/maimai"
        )

    async def get_song_list(self, uid: str = "") -> list[SongInfo]:
        """获取全曲目列表（公共 API，无需认证）。"""
        d = await self._api_get(f"{BASE_URL}/api/v0/maimai/song/list", auth=False)
        return [self._parse_song(i) for i in d.get("songs", d)]

    async def get_song(self, song_id: int) -> SongInfo:
        """获取单曲详细信息（含物量）。GET /api/v0/maimai/song/{song_id}"""
        d = await self._api_get(f"{BASE_URL}/api/v0/maimai/song/{song_id}", auth=False)
        return self._parse_song(d)

    async def get_alias_list(self) -> dict[int, list[str]]:
        """获取别名列表。GET /api/v0/maimai/alias/list"""
        d = await self._api_get(f"{BASE_URL}/api/v0/maimai/alias/list", auth=False)
        result: dict[int, list[str]] = {}
        for item in d.get("aliases", []):
            sid = item.get("song_id", 0)
            aliases = item.get("aliases", [])
            if sid and aliases:
                result[sid] = aliases
        return result

    async def get_player_info(self, fc: str = "", uid: str = "") -> PlayerInfo:
        """获取玩家基本信息。API Key 模式通过 fc 查询，OAuth 模式通过当前 uid 查询。"""
        b = self._endpoint_base()
        u = f"{b}/player/{fc}" if not self._is_oauth and fc else f"{b}/player"
        d = await self._api_get(u, uid)
        inner = d.get("data", d)
        fc_value = inner.get("friend_code", fc)
        if not isinstance(fc_value, str):
            fc_value = str(fc_value)
        return PlayerInfo(
            name=inner.get("name", inner.get("nickname", "")),
            rating=inner.get("rating", 0),
            friend_code=fc_value,
            class_rank=inner.get("class_rank", 0),
            course_rank=inner.get("course_rank", 0),
            star=inner.get("star", 0),
            trophy=inner.get("trophy"),
            icon=inner.get("icon"),
            upload_time=inner.get("upload_time"),
        )

    async def get_b50(self, fc: str = "", uid: str = "") -> PlayerB50:
        """获取 Best 50 数据（新曲 Best 35 + 旧曲 Recent 15）。"""
        b = self._endpoint_base()
        if not self._is_oauth and fc:
            u = f"{b}/player/{fc}/bests"
        elif not self._is_oauth:
            raise ApiRequestError("开发者 API 模式需要提供 friend_code")
        else:
            u = f"{b}/player/bests"
        return self._parse_b50(await self._api_get(u, uid))

    async def get_user_profile(self, uid: str = "") -> UserProfile:
        """获取 LXNS 用户资料（OAuth 模式下的用户主键）。调用 GET /api/v0/user/profile。"""
        d = await self._api_get(f"{BASE_URL}/api/v0/user/profile", uid)
        inner = d.get("data", d)
        return UserProfile(
            id=inner.get("id", 0),
            name=inner.get("name", ""),
            email=inner.get("email", ""),
            avatar=inner.get("avatar", ""),
        )

    async def get_song_records(self, sid: int, uid: str = "") -> SongRecord:
        """获取指定歌曲的游玩记录（当前未使用）。"""
        d = await self._api_get(
            f"{self._endpoint_base()}/player/records?song_id={sid}", uid
        )
        return SongRecord(records=[self._parse_record(i) for i in d.get("records", d)])

    async def get_player_best(
        self,
        song_id: int,
        level_index: int = 0,
        song_type: str = "",
        fc: str = "",
        uid: str = "",
    ) -> Optional[Score]:
        """获取玩家在特定谱面的最佳成绩。

        Args:
            song_id: 曲目ID
            level_index: 难度索引（0-4）
            song_type: 谱面类型（standard/dx）
            fc: 好友码（API Key模式）
            uid: 用户ID（OAuth模式）
        """
        b = self._endpoint_base()
        params = [f"song_id={song_id}"]

        if level_index >= 0:
            params.append(f"level_index={level_index}")

        if song_type:
            params.append(f"song_type={song_type}")

        query = "&".join(params)

        # 使用 _is_oauth 判断模式
        if not self._is_oauth and fc:
            url = f"{b}/player/{fc}/best?{query}"
        elif not self._is_oauth:
            raise ApiRequestError("开发者 API 模式需要提供 friend_code")
        else:
            url = f"{b}/player/best?{query}"

        d = await self._api_get(url, uid)
        inner = d.get("data", d)
        return (
            self._parse_score(inner) if inner and inner.get("id") is not None else None
        )

    # --- 响应解析器 ---

    @staticmethod
    def _parse_song(item: dict) -> SongInfo:
        bi = item.get("basic_info", {})
        dd = item.get("difficulties", {})
        diff_details = []
        for chart_type in ("standard", "dx", "utage"):
            for d in dd.get(chart_type, []):
                nt = d.get("notes") or {}
                detail = {
                    "type": chart_type,
                    "difficulty": d.get("difficulty", 0),
                    "level": d.get("level", ""),
                    "level_value": d.get("level_value", 0.0),
                    "note_designer": d.get("note_designer", ""),
                    "notes": {
                        "total": nt.get("total", 0),
                        "tap": nt.get("tap", 0),
                        "hold": nt.get("hold", 0),
                        "slide": nt.get("slide", 0),
                        "touch": nt.get("touch", 0),
                        "break": nt.get("break", 0),
                    },
                }
                if chart_type == "utage":
                    detail["kanji"] = d.get("kanji", "")
                    detail["description"] = d.get("description", "")
                    detail["is_buddy"] = d.get("is_buddy", False)
                diff_details.append(detail)
        return SongInfo(
            id=item.get("id", 0),
            title=item.get("title", ""),
            artist=item.get("artist", ""),
            genre=item.get("genre", bi.get("genre", "")),
            bpm=item.get("bpm", bi.get("bpm", 0)),
            version=item.get("version", bi.get("from", 0)),
            is_utage=item.get("id", 0) > 100000,
            map=item.get("map", ""),
            difficulty_details=diff_details,
            image_url=item.get("image_url", item.get("image", "")),
            rights=item.get("rights", ""),
        )

    @staticmethod
    def _parse_record(item: dict) -> PlayerRecord:
        """将 API 响应中的单条成绩转换为 PlayerRecord 模型。level_index 支持字符串和整数。"""
        li = item.get("level_index", item.get("difficulty", 0))
        if isinstance(li, str):
            try:
                li = int(li)
            except ValueError:
                li = {
                    "basic": 0,
                    "advanced": 1,
                    "expert": 2,
                    "master": 3,
                    "remaster": 4,
                }.get(li.lower(), 0)
        return PlayerRecord(
            song_id=item.get("song_id", item.get("id", 0)),
            level_index=li,
            title=item.get("title", item.get("song_name", "")),
            difficulty="",
            level_value=item.get("level_value", item.get("level", 0.0)),
            achievement=item.get("achievements", item.get("achievement", 0)),
            dx_score=item.get("dx_score", item.get("delux_score", 0)),
            dx_rating=item.get("dx_rating", item.get("rating", 0)),
            fc=item.get("fc") or "",
            fs=item.get("fs") or "",
            rate=item.get("rate") or "",
            combo_status=item.get("combo_status") or "",
            sync_status=item.get("sync_status") or "",
            play_time=item.get("play_time", None),
        )

    @staticmethod
    def _parse_score(item: dict) -> Score:
        """将 API 响应中的单曲成绩转换为 Score 模型。"""
        li = item.get("level_index", 0)
        if isinstance(li, str):
            try:
                li = int(li)
            except ValueError:
                li = {
                    "basic": 0,
                    "advanced": 1,
                    "expert": 2,
                    "master": 3,
                    "remaster": 4,
                }.get(li.lower(), 0)
        return Score(
            id=item.get("id", 0),
            song_name=item.get("song_name", ""),
            level=item.get("level", ""),
            level_index=li,
            achievements=item.get("achievements", 0.0),
            fc=item.get("fc") or "",
            fs=item.get("fs") or "",
            dx_score=item.get("dx_score", 0),
            dx_star=item.get("dx_star", 0),
            dx_rating=item.get("dx_rating", 0.0),
            rate=item.get("rate") or "",
            type=item.get("type") or "",
            play_time=item.get("play_time"),
            upload_time=item.get("upload_time"),
            last_played_time=item.get("last_played_time"),
        )

    @classmethod
    def _parse_b50(cls, data: dict) -> PlayerB50:
        """将 Best50 接口响应转换为 PlayerB50 模型（standard 为 Best 35，dx 为 Best 15）。"""
        inner = data.get("data", data)
        return PlayerB50(
            standard_total=inner.get("standard_total", 0),
            dx_total=inner.get("dx_total", 0),
            standard=[cls._parse_score(i) for i in inner.get("standard", [])],
            dx=[cls._parse_score(i) for i in inner.get("dx", [])],
        )

    async def close(self) -> None:
        """清理资源（当前无持久连接，预留扩展）。"""
        pass
