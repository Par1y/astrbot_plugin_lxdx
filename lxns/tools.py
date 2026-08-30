"""LLM 工具实现：将插件已有查分功能暴露给大模型（function calling）。

采用官方文档推荐的 pydantic dataclass + FunctionTool 方式定义，
在插件 __init__ 中通过 context.add_llm_tools() 注册（需 AstrBot >= 4.5.1）。

约定：
- call() 返回 str：成功返回供 LLM 分析总结的文本数据；失败返回引导信息，由 LLM 转述给用户。
- 配置 llm_tool_send_image 开启时，B50/Bests/Recent/查歌 会同时发送渲染的图片卡片；
  单曲成绩仅返回文本。
- 查歌类工具使用公共歌曲接口，无需授权。
- 绑定好友码不作为 LLM 工具暴露（避免提示注入触发持久化写入），由查询工具的
  未绑定引导信息指引用户执行 /lxdx bind、/lxchu bind 指令。
"""

from typing import Any

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from .models import (
    LxnsError,
    AuthExpiredError,
    DIFFICULTY_NAMES,
    CHU_DIFFICULTY_NAMES,
    MAIMAI_DIFF_MAP,
    CHU_DIFF_MAP,
    JINJA_OPTIONS,
)


class _ToolError(Exception):
    """工具无法继续执行，message 为面向 LLM 的引导信息。"""


def _s(v) -> str:
    """LLM 参数防御性转换为字符串（部分模型的 function calling 不强制 schema 类型，
    数字型 friend_code/query/difficulty 常以 JSON number 传入）。"""
    return str(v).strip() if v is not None else ""


@dataclass
class LxdxToolBase(FunctionTool[AstrAgentContext]):
    """落雪DX LLM 工具基类：持有插件实例引用，并提供公共查询逻辑。"""

    plugin: Any = None
    """插件实例引用，供工具访问 client / storage / handler 等内部组件。"""

    # --- 目标解析与公共逻辑 ---

    async def _maimai_target(
        self, ev: AstrMessageEvent, fc_arg: str = ""
    ) -> tuple[str, str]:
        """解析舞萌查询目标。API Key 模式返回 (fc, "")，OAuth 模式返回 ("", uid)。"""
        p = self.plugin
        if p._is_oauth:
            uid = await p._restore_token(ev)
            if not uid:
                raise _ToolError(
                    "用户尚未完成 OAuth 授权。请引导用户发送指令 /lxdx login，按提示完成授权后重试"
                )
            return "", uid
        fc = str(fc_arg or "").strip() or await p._st.kv_get(
            p._st.binding_key(ev.get_sender_id())
        )
        if not fc:
            raise _ToolError(
                "用户未绑定舞萌好友码。请引导用户发送指令 /lxdx bind <好友码> 完成绑定，"
                "或向用户索取好友码后作为 friend_code 参数重试"
            )
        return fc, ""

    async def _chunithm_target(
        self, ev: AstrMessageEvent, fc_arg: str = ""
    ) -> tuple[int, str]:
        """解析中二节奏查询目标。API Key 模式返回 (fc, "")，OAuth 模式返回 (0, uid)。"""
        p = self.plugin
        if p._is_oauth:
            uid = await p._restore_token(ev)
            if not uid:
                raise _ToolError(
                    "用户尚未完成 OAuth 授权。请引导用户发送指令 /lxchu login，按提示完成授权后重试"
                )
            return 0, uid
        raw = str(fc_arg or "").strip() or await p._st.kv_get(
            p._st.chu_binding_key(ev.get_sender_id())
        )
        if not raw:
            raise _ToolError(
                "用户未绑定中二节奏好友码。请引导用户发送指令 /lxchu bind <好友码> 完成绑定，"
                "或向用户索取好友码后作为 friend_code 参数重试"
            )
        try:
            return int(raw), ""
        except (TypeError, ValueError):
            raise _ToolError("好友码格式错误，应为纯数字")

    async def _clear_expired_auth(self, uid: str) -> None:
        """OAuth 过期后的清理，与指令处理保持一致。"""
        if uid:
            p = self.plugin
            await p._st.kv_delete(p._st.token_key(uid))
            p._auth.remove_tokens(uid)

    async def _send_card(self, ev: AstrMessageEvent, tmpl: str, build_data) -> bool:
        """渲染并发送图片卡片。build_data 为异步工厂，仅在通过配置/模板门控后调用，
        避免关闭图片发送时执行封面/立绘下载等无效 I/O。失败仅记录日志并返回 False。"""
        p = self.plugin
        if not tmpl or not p._llm_send_image:
            return False
        try:
            url = await p.render_html(tmpl, await build_data(), options=JINJA_OPTIONS)
            await ev.send(ev.image_result(url))
            return True
        except Exception as e:
            logger.warning(f"[lxdx] llm tool send card failed: {e}")
            return False

    def _multi_match(self, res: list) -> str | None:
        """多结果时返回提示文本，单结果或空结果返回 None。"""
        if len(res) <= 1:
            return None
        ns = "\n".join(
            f"- {s.title} (ID:{getattr(s, 'display_id', s.id)})" for s in res[:10]
        )
        return f"找到多首匹配的歌曲，请向用户确认具体曲目后用准确名称或 ID 重试：\n{ns}"


@dataclass
class MaimaiB50Tool(LxdxToolBase):
    """查询舞萌DX Best 50 成绩。"""

    name: str = "lxdx_maimai_b50"
    description: str = (
        "查询舞萌DX（国服）玩家的 Best 50 成绩（旧版本谱面 Best 35 + 当前版本谱面 Best 15），"
        "返回 Rating 与完整成绩列表，可用于总结分析玩家水平。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "friend_code": {
                    "type": "string",
                    "description": (
                        "目标玩家的好友码，可选。未提供时使用发送者已绑定的好友码；"
                        "OAuth 授权模式下查询发送者本人。"
                    ),
                },
            },
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        ev = context.context.event
        fc_arg = _s(kwargs.get("friend_code"))
        p = self.plugin
        fc, uid = "", ""
        try:
            fc, uid = await self._maimai_target(ev, fc_arg)
            b50 = await p._client.get_b50(fc=fc, uid=uid)
            pi = await p._client.get_player_info(fc=fc, uid=uid)
        except _ToolError as e:
            return f"查询失败：{e}"
        except AuthExpiredError as e:
            await self._clear_expired_auth(uid)
            return f"查询失败：{e}"
        except LxnsError as e:
            return f"查询失败：{e}"
        except Exception as e:
            logger.error(f"[lxdx] maimai_b50 tool error: {e}")
            return "查询过程中发生未知错误"

        rating = b50.standard_total + b50.dx_total
        name = pi.name if pi else "Unknown"

        async def build_card_data() -> dict:
            return {
                "player_name": name,
                "rating": rating,
                "best": p._maimai._rec_rows(b50.standard),
                "recent": p._maimai._rec_rows(b50.dx),
            }

        card = await self._send_card(ev, p._tmpl.get("b50"), build_card_data)
        ls = [f"玩家: {name}  Rating: {rating}", "= 旧版本谱面 Best 35 ="]
        ls.extend(p._maimai._b50_rows(b50.standard))
        ls.append("= 现版本谱面 Best 15 =")
        ls.extend(p._maimai._b50_rows(b50.dx))
        prefix = "已发送成绩卡片图片。\n" if card else ""
        return prefix + "\n".join(ls)


@dataclass
class MaimaiSongTool(LxdxToolBase):
    """查询舞萌DX歌曲信息。"""

    name: str = "lxdx_maimai_song"
    description: str = (
        "查询舞萌DX歌曲信息（曲名、艺术家、BPM、各难度等级与定数、谱师等），"
        "支持歌曲名、ID 或常用别名搜索。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "歌曲名称、ID 或别名。",
                },
            },
            "required": ["query"],
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        ev = context.context.event
        q = _s(kwargs.get("query"))
        if not q:
            return "请提供要查询的歌曲名或 ID"
        p = self.plugin
        res = await p._maimai._lookup(q)
        if not res:
            return f"未找到歌曲：{q}"
        if m := self._multi_match(res):
            return m
        song = res[0]
        try:
            song = await p._client.get_song(song.id)
        except Exception as e:
            logger.warning(f"[lxdx] tool failed to fetch song detail: {e}")

        async def build_card_data() -> dict:
            return {
                "song": {
                    "title": song.title,
                    "artist": song.artist,
                    "genre": song.genre,
                    "bpm": song.bpm,
                    "display_id": song.display_id,
                    "is_utage": song.is_utage,
                    "version": song.version,
                    "map": song.map,
                },
                "jacket_data_uri": await p._am.get_jacket_data_uri(song.id) or "",
                "difficulties": p._maimai._diff_rows(song),
            }

        card = await self._send_card(
            ev, p._tmpl.get("song_info"), build_card_data
        )
        prefix = "已发送歌曲信息卡片图片。\n" if card else ""
        return prefix + p._maimai._song_text(song)


@dataclass
class MaimaiScoreTool(LxdxToolBase):
    """查询舞萌DX单曲成绩。"""

    name: str = "lxdx_maimai_score"
    description: str = (
        "查询玩家在指定舞萌DX歌曲上的最佳成绩（达成率、评级、DX分数等）。"
        "不指定难度时返回各难度最佳成绩。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "歌曲名称、ID 或别名。",
                },
                "difficulty": {
                    "type": "string",
                    "description": (
                        "难度，可选。basic/advanced/expert/master/remaster、"
                        "缩写（bas/adv/exp/mas/rem）或数字 0-4；不提供时查询所有难度。"
                    ),
                },
                "song_type": {
                    "type": "string",
                    "description": "谱面类型，可选。std（标准谱）或 dx（DX 谱）；不提供时自动选择。",
                },
            },
            "required": ["query"],
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        ev = context.context.event
        q = _s(kwargs.get("query"))
        if not q:
            return "请提供要查询的歌曲名或 ID"
        p = self.plugin
        fc, uid = "", ""
        try:
            fc, uid = await self._maimai_target(ev)
            res = await p._maimai._lookup(q, uid)
        except _ToolError as e:
            return f"查询失败：{e}"
        if not res:
            return f"未找到歌曲：{q}"
        if m := self._multi_match(res):
            return m
        song = res[0]
        try:
            song = await p._client.get_song(song.id)
        except Exception as e:
            logger.warning(f"[lxdx] tool failed to fetch song detail: {e}")

        lv = MAIMAI_DIFF_MAP.get(_s(kwargs.get("difficulty")).lower(), -1)
        st = _s(kwargs.get("song_type")).lower()
        st = "dx" if st == "dx" else ("standard" if st in ("std", "standard") else "")

        try:
            if lv == -1:
                found = await p._maimai._query_all_difficulties(song, fc, uid)
                if not found:
                    return f"未找到成绩：{song.title}"
                found.sort(key=lambda x: x[0], reverse=True)
                found = found[:3]
                lines = [f"{song.title} 成绩（各难度最佳）："]
                for idx, sc, chart_type in found:
                    diff = DIFFICULTY_NAMES[idx]
                    tdisp = {
                        "standard": "STD",
                        "dx": "DX",
                        "utage": "UTAGE",
                    }.get(chart_type, chart_type.upper())
                    lines.append(
                        f"{tdisp} {diff} {sc.level}: "
                        f"{sc.achievements:.4f}% ({sc.rate.upper()})"
                    )
                return "\n".join(lines)

            if not st:
                matching = [
                    d for d in song.difficulty_details if d["difficulty"] == lv
                ]
                if not matching:
                    return f"该歌曲没有此难度：{song.title}"
                st = next(
                    (d["type"] for d in matching if d["type"] == "standard"),
                    matching[0]["type"],
                )
            sc = await p._client.get_player_best(
                song_id=song.id, level_index=lv, song_type=st, fc=fc, uid=uid
            )
            if not sc:
                return f"未找到成绩：{song.title} {DIFFICULTY_NAMES[lv]}"
            return p._maimai._score_text(song, sc, lv)
        except AuthExpiredError as e:
            await self._clear_expired_auth(uid)
            return f"查询失败：{e}"
        except LxnsError as e:
            return f"查询失败：{e}"
        except Exception as e:
            logger.error(f"[lxdx] maimai_score tool error: {e}")
            return "查询过程中发生未知错误"


@dataclass
class ChunithmBestsTool(LxdxToolBase):
    """查询中二节奏 Best 30 + Selection 10 + New 20。"""

    name: str = "lxdx_chunithm_bests"
    description: str = (
        "查询中二节奏（国服）玩家的 Best 30 + Selection 10 + New 20 成绩，"
        "返回 Rating 与完整成绩列表，可用于总结分析玩家水平。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "friend_code": {
                    "type": "string",
                    "description": (
                        "目标玩家的好友码，可选。未提供时使用发送者已绑定的好友码；"
                        "OAuth 授权模式下查询发送者本人。"
                    ),
                },
            },
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        ev = context.context.event
        fc_arg = _s(kwargs.get("friend_code"))
        p = self.plugin
        fc, uid = 0, ""
        try:
            fc, uid = await self._chunithm_target(ev, fc_arg)
            bests = await p._chu_client.get_bests(fc=fc, uid=uid)
            pi = await p._chu_client.get_player_info(fc=fc, uid=uid)
        except _ToolError as e:
            return f"查询失败：{e}"
        except AuthExpiredError as e:
            await self._clear_expired_auth(uid)
            return f"查询失败：{e}"
        except LxnsError as e:
            return f"查询失败：{e}"
        except Exception as e:
            logger.error(f"[lxdx] chunithm_bests tool error: {e}")
            return "查询过程中发生未知错误"

        async def build_card_data() -> dict:
            return {
                "player_name": pi.name,
                "rating": pi.rating,
                "friend_code": pi.friend_code,
                "character_uri": await p._chunithm._get_character_uri(pi),
                "bests": p._chunithm._chu_score_rows(bests.bests),
                "selections": p._chunithm._chu_score_rows(bests.selections),
                "new_bests": p._chunithm._chu_score_rows(bests.new_bests),
            }

        card = await self._send_card(
            ev, p._tmpl.get("chunithm_b50"), build_card_data
        )
        prefix = "已发送成绩卡片图片。\n" if card else ""
        return prefix + p._chunithm._chu_bests_text(pi, bests)


@dataclass
class ChunithmRecentTool(LxdxToolBase):
    """查询中二节奏 Recent 50。"""

    name: str = "lxdx_chunithm_recent"
    description: str = "查询中二节奏（国服）玩家最近的游玩记录（Recent 50）。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "friend_code": {
                    "type": "string",
                    "description": (
                        "目标玩家的好友码，可选。未提供时使用发送者已绑定的好友码；"
                        "OAuth 授权模式下查询发送者本人。"
                    ),
                },
            },
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        ev = context.context.event
        fc_arg = _s(kwargs.get("friend_code"))
        p = self.plugin
        fc, uid = 0, ""
        try:
            fc, uid = await self._chunithm_target(ev, fc_arg)
            recents = await p._chu_client.get_recents(fc=fc, uid=uid)
            pi = await p._chu_client.get_player_info(fc=fc, uid=uid)
        except _ToolError as e:
            return f"查询失败：{e}"
        except AuthExpiredError as e:
            await self._clear_expired_auth(uid)
            return f"查询失败：{e}"
        except LxnsError as e:
            return f"查询失败：{e}"
        except Exception as e:
            logger.error(f"[lxdx] chunithm_recent tool error: {e}")
            return "查询过程中发生未知错误"

        async def build_card_data() -> dict:
            return {
                "player_name": pi.name,
                "friend_code": pi.friend_code,
                "recent": p._chunithm._chu_score_rows(recents),
            }

        card = await self._send_card(
            ev, p._tmpl.get("chunithm_recent"), build_card_data
        )
        prefix = "已发送成绩卡片图片。\n" if card else ""
        return prefix + p._chunithm._chu_recent_text(pi, recents)


@dataclass
class ChunithmSongTool(LxdxToolBase):
    """查询中二节奏歌曲信息。"""

    name: str = "lxdx_chunithm_song"
    description: str = (
        "查询中二节奏歌曲信息（曲名、艺术家、BPM、各难度等级与定数等），"
        "支持歌曲名、ID 或常用别名搜索。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "歌曲名称、ID 或别名。",
                },
            },
            "required": ["query"],
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        ev = context.context.event
        q = _s(kwargs.get("query"))
        if not q:
            return "请提供要查询的歌曲名或 ID"
        p = self.plugin
        res = await p._chunithm._chu_lookup(q)
        if not res:
            return f"未找到歌曲：{q}"
        if m := self._multi_match(res):
            return m
        s = res[0]
        try:
            fd = await p._chu_client.get_song(s.id)
            if fd:
                s = fd
        except Exception as e:
            logger.warning(f"[lxdx] tool failed to fetch chunithm song detail: {e}")

        async def build_card_data() -> dict:
            return {
                "song": {
                    "title": s.title,
                    "artist": s.artist,
                    "genre": s.genre,
                    "bpm": s.bpm,
                    "id": s.id,
                    "map": s.map,
                },
                "jacket_data_uri": await p._am.get_chunithm_jacket_data_uri(s.id)
                or "",
                "difficulties": p._chunithm._chu_diff_rows(s),
            }

        card = await self._send_card(
            ev, p._tmpl.get("chunithm_song_info"), build_card_data
        )
        prefix = "已发送歌曲信息卡片图片。\n" if card else ""
        return prefix + p._chunithm._chu_song_text(s)


@dataclass
class ChunithmScoreTool(LxdxToolBase):
    """查询中二节奏单曲成绩。"""

    name: str = "lxdx_chunithm_score"
    description: str = (
        "查询玩家在指定中二节奏歌曲上的最佳成绩（分数、评级、Rating 等）。"
        "不指定难度时返回各难度最佳成绩。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "歌曲名称、ID 或别名。",
                },
                "difficulty": {
                    "type": "string",
                    "description": (
                        "难度，可选。basic/advanced/expert/master/ultima/worldsend、"
                        "缩写（bas/adv/exp/mas/ult/we）或数字 0-5；不提供时查询所有难度。"
                    ),
                },
            },
            "required": ["query"],
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        ev = context.context.event
        q = _s(kwargs.get("query"))
        if not q:
            return "请提供要查询的歌曲名或 ID"
        p = self.plugin
        fc, uid = 0, ""
        try:
            fc, uid = await self._chunithm_target(ev)
            res = await p._chunithm._chu_lookup(q, uid)
        except _ToolError as e:
            return f"查询失败：{e}"
        if not res:
            return f"未找到歌曲：{q}"
        if m := self._multi_match(res):
            return m
        song = res[0]

        lv = CHU_DIFF_MAP.get(_s(kwargs.get("difficulty")).lower(), -1)

        try:
            if lv == -1:
                found = await p._chunithm._chu_query_all_difficulties(song, fc, uid)
                if not found:
                    return f"未找到成绩：{song.title}"
                found.sort(key=lambda x: x[0], reverse=True)
                found = found[:3]
                lines = [f"{song.title} 成绩（各难度最佳）："]
                for idx, sc in found:
                    diff = CHU_DIFFICULTY_NAMES[idx]
                    lines.append(f"{diff} {sc.level}: {sc.score} ({sc.rank_display})")
                return "\n".join(lines)

            sc = await p._chu_client.get_player_best(
                song_id=song.id, level_index=lv, fc=fc, uid=uid
            )
            if not sc:
                return f"未找到成绩：{song.title} {CHU_DIFFICULTY_NAMES[lv]}"
            return p._chunithm._chu_score_text(song, sc, lv)
        except AuthExpiredError as e:
            await self._clear_expired_auth(uid)
            return f"查询失败：{e}"
        except LxnsError as e:
            return f"查询失败：{e}"
        except Exception as e:
            logger.error(f"[lxdx] chunithm_score tool error: {e}")
            return "查询过程中发生未知错误"
