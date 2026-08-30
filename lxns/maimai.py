"""舞萌DX 查分功能"""

from datetime import datetime, timezone, timedelta
from astrbot.api.event import AstrMessageEvent
from astrbot.api import logger

from .models import (
    LxnsError,
    AuthExpiredError,
    DIFFICULTY_NAMES,
    DIFFICULTY_SHORT,
    MAIMAI_DIFF_MAP,
    JINJA_OPTIONS,
)


def format_play_time(utc_time_str: str) -> str:
    """将UTC时间转换为GMT+8并格式化显示。

    Args:
        utc_time_str: UTC时间字符串，如 "2026-05-21T11:15:00Z"

    Returns:
        格式化的时间字符串，如 "2026-05-21 19:15"
    """
    if not utc_time_str:
        return "-"
    try:
        # 解析UTC时间
        utc_time = datetime.fromisoformat(utc_time_str.replace("Z", "+00:00"))
        # 转换为GMT+8
        gmt8 = utc_time.astimezone(timezone(timedelta(hours=8)))
        # 格式化为简化显示
        return gmt8.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return utc_time_str


class MaimaiHandler:
    def __init__(self, plugin):
        self._p = plugin

    @staticmethod
    def _args(ev: AstrMessageEvent, n: int) -> list:
        return ev.message_str.strip().split()[n:]

    # --- song helpers ---

    async def _ensure_songs(self, uid: str = ""):
        if self._p._sdb.loaded:
            return
        try:
            logger.info("[lxdx] fetching song list...")
            self._p._sdb.load_from_list(await self._p._client.get_song_list(uid))
            logger.info(f"[lxdx] loaded {self._p._sdb.song_count} songs")
            logger.info("[lxdx] fetching aliases...")
            alias_map = await self._p._client.get_alias_list()
            self._p._sdb.load_aliases(alias_map)
            logger.info(f"[lxdx] loaded {len(alias_map)} alias groups")
            self._p._sdb.save_cache()
        except Exception as e:
            logger.warning(f"[lxdx] song list fetch failed: {e}")

    async def _lookup(self, q: str, uid: str = "") -> list:
        await self._ensure_songs(uid)
        if not self._p._sdb.loaded:
            return []
        try:
            sid = int(q)
            if s := self._p._sdb.resolve_song_id(sid):
                return [s]
            return []
        except ValueError:
            return self._p._sdb.get_by_title(q)

    async def _query_all_difficulties(self, song, fc: str, uid: str) -> list:
        """查询歌曲所有难度的最佳成绩，返回 (难度索引, 成绩, 谱面类型) 列表。

        AuthExpiredError 向上传播，由调用方清理过期 token 并引导重新登录
        （指令路径与 LLM 工具共用，保证错误处理策略一致）。
        """
        found = []
        for d in song.difficulty_details:
            try:
                sc = await self._p._client.get_player_best(
                    song_id=song.id,
                    level_index=d["difficulty"],
                    song_type=d["type"],
                    fc=fc,
                    uid=uid,
                )
                if sc:
                    found.append((d["difficulty"], sc, d["type"]))
            except AuthExpiredError:
                raise
            except Exception:
                continue
        return found

    # --- /lxdx help ---

    async def help(self, ev: AstrMessageEvent):
        """显示命令列表和当前授权模式。优先使用 HTML 模板渲染图片，无模板时回退纯文本。"""
        t = self._p._tmpl.get("help")
        if t:
            if self._p._debug:
                logger.info("[lxdx] rendering help (Maimai)")
            desc = (
                "已绑定开发者 API Key"
                if not self._p._is_oauth
                else "OAuth(PKCE) 交互授权"
            )
            url = await self._p.render_html(
                t,
                {
                    "plugin_display_name": "落雪DX",
                    "plugin_version": self._p._version,
                    "auth_mode": "OAuth(PKCE)" if self._p._is_oauth else "api_key",
                    "auth_desc": desc,
                    "commands": [
                        {"name": "/lxdx bind <好友码>", "desc": "绑定玩家好友码"},
                        {
                            "name": "/lxdx b50 <好友码>",
                            "desc": "Best 50 (最佳35 + 最近15)",
                        },
                        {"name": "/lxdx song <名称/ID>", "desc": "查询歌曲信息"},
                        {
                            "name": "/lxdx login <授权码>",
                            "desc": "OAuth 授权登录",
                        },
                    ],
                },
                options=JINJA_OPTIONS,
            )
            yield ev.image_result(url)
        else:
            yield ev.plain_result(
                "/lxdx help|bind <好友码>|b50 <好友码>|song <名称/ID>|login <授权码>"
            )

    # --- /lxdx bind ---

    async def bind(self, ev: AstrMessageEvent):
        """绑定好友码：/lxdx bind <friend_code>。API Key 模式会验证好友码有效性。"""
        args = self._args(ev, 2)
        if not args:
            yield ev.plain_result("用法: /lxdx bind <friend_code>")
            return
        fc = args[0]
        uid = ev.get_sender_id()
        if not self._p._is_oauth:
            try:
                await self._p._client.get_player_info(fc)
            except Exception as e:
                yield ev.plain_result(f"绑定失败: {e}")
                return
        await self._p._st.kv_put(self._p._st.binding_key(uid), fc)
        yield ev.plain_result(f"已绑定好友码: {fc}")

    # --- /lxdx b50 ---

    async def b50(self, ev: AstrMessageEvent):
        """查询 Best 50：/lxdx b50 [friend_code]。API Key 模式必须提供或已绑定 fc。"""
        args = self._args(ev, 2)
        uid = ev.get_sender_id()
        if not self._p._is_oauth:
            fc = (
                args[0]
                if args
                else await self._p._st.kv_get(self._p._st.binding_key(uid))
            )
            if not fc:
                yield ev.plain_result("请先 /lxdx bind <好友码> 或 /lxdx b50 <好友码>")
                return
            try:
                b50 = await self._p._client.get_b50(fc=fc)
            except LxnsError as e:
                yield ev.plain_result(str(e))
                return
        else:
            u = await self._p._restore_token(ev)
            if not u:
                yield ev.plain_result("请先 /lxdx login 授权")
                return
            try:
                b50 = await self._p._client.get_b50(uid=u)
            except AuthExpiredError as e:
                await self._p._st.kv_delete(self._p._st.token_key(u))
                self._p._auth.remove_tokens(u)
                yield ev.plain_result(str(e))
                return
            except LxnsError as e:
                yield ev.plain_result(str(e))
                return

        t = self._p._tmpl.get("b50")
        if t:
            if self._p._debug:
                logger.info("[lxdx] rendering b50")
            pi = await self._p._client.get_player_info(
                fc=fc if not self._p._is_oauth else "",
                uid=u if self._p._is_oauth else "",
            )
            url = await self._p.render_html(
                t,
                {
                    "player_name": pi.name if pi else "",
                    "rating": (b50.standard_total + b50.dx_total) if b50 else 0,
                    "best": self._rec_rows(b50.standard),
                    "recent": self._rec_rows(b50.dx),
                },
                options=JINJA_OPTIONS,
            )
            yield ev.image_result(url)
        else:
            yield ev.plain_result(self._b50_text(b50))

    # --- /lxdx song ---

    async def song(self, ev: AstrMessageEvent):
        """查询歌曲信息：/lxdx song <名称 或 ID>。多个匹配时返回列表。"""
        args = self._args(ev, 2)
        if not args:
            yield ev.plain_result("用法: /lxdx song <名称 或 ID>")
            return
        q = " ".join(args)
        uid = ev.get_sender_id()
        if self._p._is_oauth:
            uid = await self._p._restore_token(ev) or ""
        res = await self._lookup(q, uid)
        if not res:
            yield ev.plain_result(f"未找到: {q}")
            return
        if len(res) > 1:
            ns = "\n".join(f"  · {s.title} (ID:{s.display_id})" for s in res[:10])
            yield ev.plain_result(f"多个结果:\n{ns}")
            return
        s = res[0]
        try:
            s = await self._p._client.get_song(s.id)
        except Exception as e:
            logger.warning(f"[lxdx] failed to fetch detailed song info: {e}")
        t = self._p._tmpl.get("song_info")
        if t:
            if self._p._debug:
                logger.info(f"[lxdx] rendering song_info for {s.title}")
            uri = await self._p._am.get_jacket_data_uri(s.id) or ""
            url = await self._p.render_html(
                t,
                {
                    "song": {
                        "title": s.title,
                        "artist": s.artist,
                        "genre": s.genre,
                        "bpm": s.bpm,
                        "display_id": s.display_id,
                        "is_utage": s.is_utage,
                        "version": s.version,
                        "map": s.map,
                    },
                    "jacket_data_uri": uri,
                    "difficulties": self._diff_rows(s),
                },
                options=JINJA_OPTIONS,
            )
            yield ev.image_result(url)
        else:
            yield ev.plain_result(self._song_text(s))

    # --- /lxdx score ---

    async def score(self, ev: AstrMessageEvent):
        """查询单曲成绩：/lxdx score <歌曲名> [难度] [类型]。

        难度: 0-4 或 basic/advanced/expert/master/remaster (可选，不指定时查询所有难度)
        类型: std/dx (可选)
        """
        args = self._args(ev, 2)
        if not args:
            yield ev.plain_result(
                "用法: /lxdx score <歌曲名> [难度] [类型]\n难度: 0-4 或 basic/advanced/expert/master/remaster (可选)\n类型: std/dx (可选)"
            )
            return

        # 从后往前解析参数：最后一个可能是类型，倒数第二个可能是难度
        song_type = ""
        level_index = -1
        song_query_parts = args[:]

        # 检查最后一个参数是否是类型
        if args[-1].lower() in ("std", "dx", "standard"):
            song_type = "dx" if args[-1].lower() == "dx" else "standard"
            song_query_parts = args[:-1]

        # 检查倒数第一个（或倒数第二个如果有类型）参数是否是难度
        if song_query_parts and song_query_parts[-1].lower() in MAIMAI_DIFF_MAP:
            level_index = MAIMAI_DIFF_MAP[song_query_parts[-1].lower()]
            song_query_parts = song_query_parts[:-1]

        # 剩余部分是歌曲名
        if not song_query_parts:
            yield ev.plain_result("请提供歌曲名")
            return

        q = " ".join(song_query_parts)

        # 查找歌曲（复用查歌逻辑，兼容别名）
        uid = ev.get_sender_id()
        if self._p._is_oauth:
            uid = await self._p._restore_token(ev) or ""
            if not uid:
                yield ev.plain_result("请先登录: /lxdx login")
                return

        res = await self._lookup(q, uid)
        if not res:
            yield ev.plain_result(f"未找到歌曲: {q}")
            return
        if len(res) > 1:
            ns = "\n".join(f"  · {s.title} (ID:{s.display_id})" for s in res[:10])
            yield ev.plain_result(f"多个结果，请使用ID查询:\n{ns}")
            return

        song = res[0]
        try:
            song = await self._p._client.get_song(song.id)
        except Exception as e:
            logger.warning(f"[lxdx] failed to fetch detailed song info: {e}")

        # 获取好友码（仅API Key模式需要）
        fc = ""
        if not self._p._is_oauth:
            fc = await self._p._st.kv_get(self._p._st.binding_key(uid))
            if not fc:
                yield ev.plain_result("请先绑定好友码: /lxdx bind <好友码>")
                return

        # 如果未指定难度，查询所有存在的难度
        if level_index == -1:
            try:
                found_scores = await self._query_all_difficulties(song, fc, uid)
            except AuthExpiredError as e:
                await self._p._st.kv_delete(self._p._st.token_key(uid))
                self._p._auth.remove_tokens(uid)
                yield ev.plain_result(str(e))
                return

            if not found_scores:
                yield ev.plain_result(f"未找到成绩: {song.title}")
                return

            # 按难度索引降序排序（难度越高索引越大），取前三个
            found_scores.sort(key=lambda x: x[0], reverse=True)
            found_scores = found_scores[:3]

            # 获取玩家信息及icon
            icon_uri = ""
            try:
                pi = await self._p._client.get_player_info(fc=fc, uid=uid)
                if self._p._debug:
                    logger.info(
                        f"[lxdx] Maimai player info fetched: name={pi.name if pi else 'None'}, rating={pi.rating if pi else 0}"
                    )
                if pi and pi.icon and isinstance(pi.icon, dict):
                    icon_id = pi.icon.get("id", 0)
                    if icon_id:
                        if self._p._debug:
                            logger.info(f"[lxdx] fetching Maimai icon for id={icon_id}")
                        icon_uri = await self._p._am.get_maimai_icon_data_uri(icon_id)
            except Exception as e:
                logger.warning(f"[lxdx] failed to fetch player info: {e}")
                pi = None

            # 渲染多难度模板
            t = self._p._tmpl.get("song_score")
            if t:
                if self._p._debug:
                    logger.info(
                        f"[lxdx] rendering song_score for {song.title} (multiple difficulties)"
                    )
                uri = await self._p._am.get_jacket_data_uri(song.id) or ""

                # 构建scores数组
                scores_data = []
                for idx, score, chart_type in found_scores:
                    scores_data.append(
                        {
                            "type": score.type,
                            "difficulty_name": DIFFICULTY_NAMES[idx]
                            if idx < len(DIFFICULTY_NAMES)
                            else f"LV{idx}",
                            "difficulty_class": DIFFICULTY_NAMES[idx]
                            .lower()
                            .replace(":", "")
                            if idx < len(DIFFICULTY_NAMES)
                            else "",
                            "level": score.level,
                            "achievements": score.achievements,
                            "rate": score.rate,
                            "dx_score": score.dx_score,
                            "dx_rating": score.dx_rating,
                            "fc": score.fc,
                            "fs": score.fs,
                            "dx_star": score.dx_star,
                            "play_time": format_play_time(score.play_time)
                            if score.play_time
                            else "-",
                        }
                    )

                url = await self._p.render_html(
                    t,
                    {
                        "song": {
                            "title": song.title,
                        },
                        "player": {
                            "name": pi.name if pi else "Unknown",
                            "rating": pi.rating if pi else 0,
                        },
                        "scores": scores_data,
                        "jacket_data_uri": uri,
                        "icon_uri": icon_uri,
                    },
                    options=JINJA_OPTIONS,
                )
                yield ev.image_result(url)
            else:
                # 纯文本输出
                lines = [f"{song.title} 成绩:"]
                for idx, score, chart_type in found_scores:
                    diff_name = (
                        DIFFICULTY_NAMES[idx]
                        if idx < len(DIFFICULTY_NAMES)
                        else f"LV{idx}"
                    )
                    type_display = {
                        "standard": "STD",
                        "dx": "DX",
                        "utage": "UTAGE",
                    }.get(chart_type, chart_type.upper())
                    lines.append(
                        f"{type_display} {diff_name} {score.level}: {score.achievements:.4f}% ({score.rate.upper()})"
                    )
                yield ev.plain_result("\n".join(lines))
            return

        # 查询指定难度的成绩
        if not song_type:
            matching = [
                d for d in song.difficulty_details if d["difficulty"] == level_index
            ]
            if not matching:
                yield ev.plain_result(
                    f"未找到难度: {song.title} {DIFFICULTY_NAMES[level_index] if level_index < len(DIFFICULTY_NAMES) else f'LV{level_index}'}"
                )
                return
            song_type = next(
                (d["type"] for d in matching if d["type"] == "standard"),
                matching[0]["type"],
            )
        try:
            score = await self._p._client.get_player_best(
                song_id=song.id,
                level_index=level_index,
                song_type=song_type,
                fc=fc,
                uid=uid,
            )
            if not score:
                yield ev.plain_result(
                    f"未找到成绩: {song.title} {DIFFICULTY_NAMES[level_index]}"
                )
                return

            # 获取玩家信息及icon
            icon_uri = ""
            try:
                pi = await self._p._client.get_player_info(fc=fc, uid=uid)
                if self._p._debug:
                    logger.info(
                        f"[lxdx] Maimai player info fetched: name={pi.name if pi else 'None'}, rating={pi.rating if pi else 0}"
                    )
                if pi and pi.icon and isinstance(pi.icon, dict):
                    icon_id = pi.icon.get("id", 0)
                    if icon_id:
                        if self._p._debug:
                            logger.info(f"[lxdx] fetching Maimai icon for id={icon_id}")
                        icon_uri = await self._p._am.get_maimai_icon_data_uri(icon_id)
            except Exception as e:
                logger.warning(f"[lxdx] failed to fetch player info: {e}")
                pi = None

            # 渲染模板
            t = self._p._tmpl.get("song_score")
            if t:
                if self._p._debug:
                    logger.info(f"[lxdx] rendering song_score for {song.title}")
                uri = await self._p._am.get_jacket_data_uri(song.id) or ""
                url = await self._p.render_html(
                    t,
                    {
                        "song": {
                            "title": song.title,
                        },
                        "player": {
                            "name": pi.name if pi else "Unknown",
                            "rating": pi.rating if pi else 0,
                        },
                        "scores": [
                            {
                                "type": score.type,
                                "difficulty_name": DIFFICULTY_NAMES[level_index],
                                "difficulty_class": DIFFICULTY_NAMES[level_index]
                                .lower()
                                .replace(":", ""),
                                "level": score.level,
                                "achievements": score.achievements,
                                "rate": score.rate,
                                "dx_score": score.dx_score,
                                "dx_rating": score.dx_rating,
                                "fc": score.fc,
                                "fs": score.fs,
                                "dx_star": score.dx_star,
                                "play_time": format_play_time(score.play_time)
                                if score.play_time
                                else "-",
                            }
                        ],
                        "jacket_data_uri": uri,
                    },
                    options=JINJA_OPTIONS,
                )
                yield ev.image_result(url)
            else:
                yield ev.plain_result(self._score_text(song, score, level_index))
        except LxnsError as e:
            yield ev.plain_result(f"查询失败: {e}")
        except Exception as e:
            logger.error(f"[lxdx] score query error: {e}")
            yield ev.plain_result("查询过程中发生错误")

    # --- helpers ---

    @staticmethod
    def _rec_rows(recs: list) -> list:
        return [
            {
                "title": r.title if hasattr(r, "title") else r.song_name,
                "difficulty_short": DIFFICULTY_SHORT[r.level_index]
                if r.level_index < 5
                else "?",
                "difficulty_css": DIFFICULTY_NAMES[r.level_index]
                .lower()
                .replace(":", "")
                if r.level_index < 5
                else "",
                "achievement_pct": r.achievement_pct,
                "dx_score": r.dx_score,
                "level_value": r.level if hasattr(r, "level") else r.level_value,
                "rank": r.rank_display,
            }
            for r in recs
        ]

    @staticmethod
    def _diff_rows(song):
        return [
            {
                "type": d["type"],
                "name": (
                    DIFFICULTY_NAMES[d["difficulty"]]
                    if d["difficulty"] < len(DIFFICULTY_NAMES)
                    else f"LV{d['difficulty']}"
                ),
                "css_class": (
                    DIFFICULTY_NAMES[d["difficulty"]].lower().replace(":", "")
                    if d["difficulty"] < len(DIFFICULTY_NAMES)
                    else ""
                ),
                "level": d["level"],
                "level_value": d["level_value"],
                "note_designer": d["note_designer"],
                "notes": d["notes"],
            }
            for d in song.difficulty_details
        ]

    @staticmethod
    def _b50_rows(recs) -> list[str]:
        """将 Best 35 / Best 15 记录格式化为紧凑文本行（指令回退文本与 LLM 工具共用）。"""
        ls = []
        for i, r in enumerate(recs, 1):
            d = DIFFICULTY_SHORT[r.level_index] if r.level_index < 5 else "?"
            ls.append(
                f"#{i} {r.song_name} [{d}{r.level}] "
                f"{r.achievement_pct:.4f}% DX:{r.dx_score} {r.rank_display}"
            )
        return ls

    @staticmethod
    def _b50_text(b50) -> str:
        total = b50.standard_total + b50.dx_total
        ls = [f"Rating: {total}", "= 旧版本谱面 Best 35 ="]
        ls.extend(MaimaiHandler._b50_rows(b50.standard))
        ls.append("= 现版本谱面 Best 15 =")
        ls.extend(MaimaiHandler._b50_rows(b50.dx))
        return "\n".join(ls)

    @staticmethod
    def _song_text(song) -> str:
        ls = [
            f"{song.title}  [{song.genre}]",
            f"艺术家:{song.artist}  BPM:{song.bpm}  ID:{song.display_id}"
            + (" (宴)" if song.is_utage else ""),
            "",
            "类型  难度        等级  定数  谱师",
        ]
        for d in song.difficulty_details:
            idx = d["difficulty"]
            name = DIFFICULTY_NAMES[idx] if idx < len(DIFFICULTY_NAMES) else f"LV{idx}"
            lv = d["level_value"]
            chart = "DX" if d["type"] == "dx" else "STD"
            designer = d["note_designer"] or "-"
            ls.append(
                f"  {chart:<3} {name:<8} {d['level']:>4}  {lv:.1f}  {designer}"
                if lv > 0
                else f"  {chart:<3} {name:<8} {d['level']:>4}  -  {designer}"
            )
        return "\n".join(ls)

    @staticmethod
    def _score_text(song, score, level_index) -> str:
        """生成单曲成绩的纯文本输出。"""
        chart_type = "DX" if score.type == "dx" else "STD"
        diff_name = (
            DIFFICULTY_NAMES[level_index]
            if level_index < len(DIFFICULTY_NAMES)
            else f"LV{level_index}"
        )

        ls = [
            f"{song.title}",
            f"{chart_type} {diff_name} {score.level}",
            "",
            f"达成率: {score.achievements:.4f}%",
            f"评级: {score.rate.upper()}",
            f"DX分数: {score.dx_score}",
            f"DX Rating: {int(score.dx_rating)}",
            f"FULL COMBO: {score.fc.upper() if score.fc else '-'}",
            f"FULL SYNC: {score.fs.upper() if score.fs else '-'}",
            f"DX星级: {'★' * score.dx_star if score.dx_star > 0 else '-'}",
            f"游玩时间: {score.play_time if score.play_time else '-'}",
        ]
        return "\n".join(ls)
