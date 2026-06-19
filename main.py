"""落雪DX 插件入口。国服舞萌 DX / 中二节奏查分，支持 OAuth(PKCE) 和开发者 API Key。"""

from dataclasses import asdict
from pathlib import Path

import jinja2

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

from .lxns.auth import LxnsAuth
from .lxns.client import LxnsClient
from .lxns.chunithm_client import ChunithmClient
from .lxns.maimai import MaimaiHandler
from .lxns.chunithm import ChunithmHandler
from .lxns.models import TokenInfo, LxnsError
from .utils.storage import StorageManager
from .utils.song_db import SongDatabase
from .utils.chunithm_song_db import ChuSongDatabase
from .utils.assets import AssetManager

SCOPE = "read_player read_user_profile"


@register(
    "astrbot_plugin_lxdx",
    "Par1y",
    "国服舞萌DX/中二节奏插件，使用落雪接口，支持 b50、曲目信息等功能。",
    "0.4.2",
)
class LxdxPlugin(Star):
    """国服舞萌 DX / 中二节奏查分插件。

    支持两种认证模式：
    - OAuth(PKCE): 用户通过浏览器授权，适合多用户使用（无需开发者API Key）
    - API Key: 开发者直接使用落雪 API Key，无需 OAuth 授权流程
    """

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        c = config or {}

        dp = self._data_path(context)
        self._debug = c.get("debug", False)
        self._st = StorageManager(self, dp, debug=self._debug)
        self._sdb = SongDatabase(self._st.cache_dir)
        self._chu_sdb = ChuSongDatabase(self._st.cache_dir)
        self._am = AssetManager(self._st.assets_dir, debug=self._debug)

        self._auth = LxnsAuth(c.get("client_id", ""))
        self._ru = c.get("redirect_uri", "")
        self._method = c.get("method", "OAuth")
        self._api_key = c.get("api_key", "")
        self._client = LxnsClient(
            self._auth,
            debug=self._debug,
            redirect_uri=self._ru,
            api_key=self._api_key,
            is_oauth=self._method != "api_key",
            on_token_refresh=self._persist_token,
        )
        self._chu_client = ChunithmClient(
            self._auth,
            debug=self._debug,
            redirect_uri=self._ru,
            api_key=self._api_key,
            is_oauth=self._method != "api_key",
            on_token_refresh=self._persist_token,
        )
        self._am = AssetManager(self._st.assets_dir, debug=self._debug)

        self._jinja_env = jinja2.Environment(
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self._pkce: dict[str, dict] = {}
        self._tmpl: dict[str, str] = {}
        self._tdir = Path(__file__).parent / "templates"

        self._maimai = MaimaiHandler(self)
        self._chunithm = ChunithmHandler(self)

    @staticmethod
    def _data_path(ctx: Context) -> str:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            return get_astrbot_data_path()
        except ImportError:
            pass
        return getattr(ctx, "data_dir", str(Path(__file__).parent / "data"))

    async def initialize(self):
        self._load_tmpl()
        self._st.ensure_dirs()
        if self._sdb.load_cache():
            logger.info(f"[lxdx] loaded {self._sdb.song_count} Maimai songs from cache")
        else:
            logger.info("[lxdx] no Maimai song cache, will fetch on first use")
        if self._chu_sdb.load_cache():
            logger.info(
                f"[lxdx] loaded {self._chu_sdb.song_count} Chunithm songs from cache"
            )
        else:
            logger.info("[lxdx] no Chunithm song cache, will fetch on first use")
        mode_label = "OAuth(PKCE)" if self._is_oauth else "api_key"
        logger.info(f"[lxdx] init done, mode={mode_label}")

    async def terminate(self):
        await self._st.kv_clear_all()
        await self._client.close()
        await self._chu_client.close()
        logger.info("[lxdx] terminated")

    def _load_tmpl(self):
        for n in (
            "help",
            "b50",
            "song_info",
            "song_score",
            "chunithm_help",
            "chunithm_b50",
            "chunithm_song_info",
            "chunithm_song_score",
            "chunithm_recent",
        ):
            p = self._tdir / f"{n}.html"
            if p.exists():
                self._tmpl[n] = p.read_text("utf-8")

    @property
    def _is_oauth(self) -> bool:
        return self._method != "api_key"

    # --- auth helpers ---

    async def render_html(
        self, tmpl_str: str, tmpl_data: dict, options: dict | None = None
    ):
        import hashlib
        import base64

        # 读取本地icon.png并转换为base64
        icon_path = Path(__file__).parent / "logo.png"
        local_icon_base64 = ""
        if icon_path.exists():
            try:
                icon_data = icon_path.read_bytes()
                local_icon_base64 = base64.b64encode(icon_data).decode("ascii")
                if self._debug:
                    logger.info(f"[lxdx] loaded local icon: {len(icon_data)} bytes")
            except Exception as e:
                logger.warning(f"[lxdx] failed to load local icon: {e}")

        # 将base64图标注入模板数据
        tmpl_data_with_icon = tmpl_data.copy()
        tmpl_data_with_icon["local_icon_base64"] = local_icon_base64

        rendered = self._jinja_env.from_string(tmpl_str).render(**tmpl_data_with_icon)
        if self._debug:
            html_hash = hashlib.md5(rendered.encode()).hexdigest()
            head = rendered[:200].replace("\n", "\\n")
            logger.info(
                f"[lxdx] render_html: len={len(rendered)} md5={html_hash}\n"
                f"  options={options}\n"
                f"  head: {head}\n"
            )
            dump = Path(self._st.cache_dir) / f"render_debug_{html_hash}.html"
            dump.write_text(rendered, "utf-8")
            logger.info(f"[lxdx] dumped rendered html to: {dump}")
        return await self.html_render(rendered, {}, True, options)

    async def _persist_token(self, uid: str, token: TokenInfo) -> None:
        """Token 刷新回调：将新 Token 持久化到 KV 存储。"""
        if self._debug:
            logger.info(f"[lxdx] persist token for uid={uid}")
        await self._st.kv_put(self._st.token_key(uid), asdict(token))

    async def _restore_token(self, ev: AstrMessageEvent) -> str:
        """从 KV 恢复用户 OAuth Token 到内存缓存。成功返回 uid，失败返回空字符串。"""
        uid = ev.get_sender_id()
        td = await self._st.kv_get(self._st.token_key(uid))
        if not td or not isinstance(td, dict):
            return ""
        try:
            t = TokenInfo(**td)
            self._auth.store_tokens(uid, t)
            if self._debug:
                logger.info(f"[lxdx] restored token for uid={uid}")
            return uid
        except Exception:
            await self._st.kv_delete(self._st.token_key(uid))
            return ""

    # --- command router ---

    @staticmethod
    def _args(ev: AstrMessageEvent, n: int) -> list:
        return ev.message_str.strip().split()[n:]

    @filter.command_group("lxdx")
    def lxdx_group(self):
        """落雪DX舞萌指令组"""
        pass

    @filter.command_group("lxchu")
    def lxchu_group(self):
        """落雪DX中二节奏指令组"""
        pass

    # --- /lxdx help ---

    @lxdx_group.command("help")
    async def _help(self, ev: AstrMessageEvent):
        """显示命令列表和当前授权模式。"""
        async for r in self._maimai.help(ev):
            yield r

    # --- /lxdx bind ---

    @lxdx_group.command("bind")
    async def _bind(self, ev: AstrMessageEvent):
        """绑定舞萌好友码：/lxdx bind <friend_code>。"""
        async for r in self._maimai.bind(ev):
            yield r

    # --- /lxdx b50 ---

    @lxdx_group.command("b50")
    async def _b50(self, ev: AstrMessageEvent):
        """查询 Best 50（最佳35+最近15）：/lxdx b50 [friend_code]。"""
        async for r in self._maimai.b50(ev):
            yield r

    # --- /lxdx song ---

    @lxdx_group.command("song")
    async def _song(self, ev: AstrMessageEvent):
        """查询舞萌歌曲信息：/lxdx song <名称 或 ID>。"""
        async for r in self._maimai.song(ev):
            yield r

    # --- /lxdx score ---

    @lxdx_group.command("score")
    async def _score(self, ev: AstrMessageEvent):
        """查询单曲成绩：/lxdx score <歌曲名/ID> <难度> [类型]。"""
        async for r in self._maimai.score(ev):
            yield r

    # --- login ---

    async def _do_login(
        self,
        ev: AstrMessageEvent,
        cmd: str,
        client,
        bind_key,
        fallback: str,
        label: str,
    ):
        """OAuth(PKCE) 登录通用实现。"""
        args = self._args(ev, 2)
        if not self._is_oauth:
            yield ev.plain_result(f"API Key 模式无需 OAuth，直接 {cmd} bind <好友码>")
            return
        uid = ev.get_sender_id()

        if args:
            code = args[0]
            pk = self._pkce.get(uid)
            if not pk:
                yield ev.plain_result(f"未找到授权会话，请先 {cmd} login")
                return
            try:
                tok = await self._client.exchange_code(code, pk["verifier"])
                self._auth.store_tokens(uid, tok)
                await self._st.kv_put(self._st.token_key(uid), asdict(tok))
                del self._pkce[uid]

                fc_info = ""
                try:
                    pi = await client.get_player_info(uid=uid)
                    await self._st.kv_put(bind_key(uid), str(pi.friend_code))
                    fc_info = f" 好友码:{pi.friend_code} Rating:{pi.rating}"
                except Exception as e:
                    logger.warning(f"[lxdx] {label} player info fetch failed: {e}")
                    fc_info = f" (可稍后 {cmd} {fallback})"

                yield ev.plain_result(f"授权成功！{fc_info}")
            except LxnsError as e:
                self._pkce.pop(uid, None)
                logger.error(f"[lxdx] {label} login callback failed: {e}")
                yield ev.plain_result(f"授权失败: {e}")
            except Exception as e:
                self._pkce.pop(uid, None)
                logger.error(f"[lxdx] {label} login callback error: {e}")
                yield ev.plain_result("授权过程中发生未知错误")
        else:
            pk = LxnsAuth.generate_pkce()
            self._pkce[uid] = {"verifier": pk.code_verifier}
            url = self._auth.build_authorize_url(pk, self._ru, SCOPE)
            if self._debug:
                logger.info(f"[lxdx] {label} PKCE flow started for uid={uid}")
            yield ev.plain_result(
                f"请打开链接授权:\n{url}\n\n完成后用 {cmd} login <授权码> 发送给我"
            )

    @lxdx_group.command("login")
    async def _login(self, ev: AstrMessageEvent):
        """OAuth(PKCE) 登录：/lxdx login 启动授权流程，/lxdx login <授权码> 完成回调交换 Token。"""
        async for r in self._do_login(
            ev, "/lxdx", self._client, self._st.binding_key, "b50", "Maimai"
        ):
            yield r

    @lxchu_group.command("login")
    async def _chu_login(self, ev: AstrMessageEvent):
        """中二节奏 OAuth(PKCE) 登录：/lxchu login 启动授权，/lxchu login <授权码> 完成回调交换 Token。"""
        async for r in self._do_login(
            ev,
            "/lxchu",
            self._chu_client,
            self._st.chu_binding_key,
            "b50",
            "Chunithm",
        ):
            yield r

    # --- /lxchu help ---

    @lxchu_group.command("help")
    async def _chu_help(self, ev: AstrMessageEvent):
        """显示中二节奏命令列表和当前授权模式。"""
        async for r in self._chunithm.help(ev):
            yield r

    # --- /lxchu bind ---

    @lxchu_group.command("bind")
    async def _chu_bind(self, ev: AstrMessageEvent):
        """绑定中二节奏好友码：/lxchu bind <friend_code>。"""
        async for r in self._chunithm.bind(ev):
            yield r

    # --- /lxchu b50 ---

    @lxchu_group.command("b50")
    async def _chu_b50(self, ev: AstrMessageEvent):
        """查询 Best 30 + Selection 10 + New 20：/lxchu b50 [friend_code]。"""
        async for r in self._chunithm.bests(ev):
            yield r

    # --- /lxchu recent ---

    @lxchu_group.command("recent")
    async def _chu_recent(self, ev: AstrMessageEvent):
        """查询 Recent 50 最近游玩：/lxchu recent [friend_code]。"""
        async for r in self._chunithm.recent(ev):
            yield r

    # --- /lxchu song ---

    @lxchu_group.command("song")
    async def _chu_song(self, ev: AstrMessageEvent):
        """查询中二节奏歌曲信息：/lxchu song <名称 或 ID>。"""
        async for r in self._chunithm.song(ev):
            yield r

    # --- /lxchu score ---

    @lxchu_group.command("score")
    async def _chu_score(self, ev: AstrMessageEvent):
        """查询单曲成绩：/lxchu score <歌曲名/ID> <难度>。"""
        async for r in self._chunithm.score(ev):
            yield r
