"""中二节奏 查分功能"""

from datetime import datetime, timezone, timedelta
from astrbot.api.event import AstrMessageEvent
from astrbot.api import logger

from .models import (
    LxnsError,
    AuthExpiredError,
    CHU_DIFFICULTY_NAMES,
    CHU_DIFFICULTY_SHORT,
    CHU_DIFF_MAP,
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


class ChunithmHandler:
    def __init__(self, plugin):
        self._p = plugin

    async def _get_character_uri(self, pi) -> str:
        """获取玩家角色立绘 Data URI，磁盘缓存由 AssetManager 处理。"""
        if not pi or not pi.character or not isinstance(pi.character, dict):
            return ""
        char_id = pi.character.get("id", 0)
        if not char_id:
            return ""
        try:
            return await self._p._am.get_chunithm_character_data_uri(char_id)
        except Exception as e:
            logger.warning(f"[lxdx] failed to fetch Chunithm character: {e}")
            return ""

    @staticmethod
    def _args(ev: AstrMessageEvent, n: int) -> list:
        return ev.message_str.strip().split()[n:]

    # --- song helpers ---

    async def _ensure_chu_songs(self, uid: str = ""):
        if self._p._chu_sdb.loaded:
            return
        try:
            logger.info("[lxdx] fetching Chunithm song list...")
            res = await self._p._chu_client.get_song_list(uid)
            self._p._chu_sdb.load_from_list(res.songs)
            logger.info(f"[lxdx] loaded {self._p._chu_sdb.song_count} Chunithm songs")
            logger.info("[lxdx] fetching Chunithm aliases...")
            alias_map = await self._p._chu_client.get_alias_list()
            self._p._chu_sdb.load_aliases(alias_map)
            logger.info(f"[lxdx] loaded {len(alias_map)} Chunithm alias groups")
            self._p._chu_sdb.save_cache()
        except Exception as e:
            logger.warning(f"[lxdx] Chunithm song list fetch failed: {e}")

    async def _chu_lookup(self, q: str, uid: str = "") -> list:
        await self._ensure_chu_songs(uid)
        if not self._p._chu_sdb.loaded:
            return []
        try:
            sid = int(q)
            if s := self._p._chu_sdb.get_by_id(sid):
                return [s]
            return []
        except ValueError:
            return self._p._chu_sdb.get_by_title(q)

    async def _chu_query_all_difficulties(self, song, fc: int, uid: str) -> list:
        """查询歌曲所有难度(0-5)的最佳成绩，返回 (难度索引, 成绩) 列表。

        AuthExpiredError 向上传播，由调用方清理过期 token 并引导重新登录
        （指令路径与 LLM 工具共用，保证错误处理策略一致）。
        """
        found = []
        for idx in range(6):  # 0-5: basic, advanced, expert, master, ultima, worldsend
            try:
                sc = await self._p._chu_client.get_player_best(
                    song_id=song.id, level_index=idx, fc=fc, uid=uid
                )
                if sc:
                    found.append((idx, sc))
            except AuthExpiredError:
                raise
            except Exception:
                continue
        return found

    # --- static helpers ---

    @staticmethod
    def _chu_score_rows(scores: list) -> list:
        result = []
        for r in scores:
            if r.level_index < len(CHU_DIFFICULTY_SHORT):
                d_short = CHU_DIFFICULTY_SHORT[r.level_index]
                d_css = (
                    CHU_DIFFICULTY_NAMES[r.level_index]
                    .lower()
                    .replace("'", "")
                    .replace(" ", "-")
                )
            else:
                d_short = "?"
                d_css = ""
            result.append(
                {
                    "song_name": r.song_name,
                    "difficulty_short": d_short,
                    "difficulty_css": d_css,
                    "level": r.level,
                    "score": r.score,
                    "rating": r.rating,
                    "rank": r.rank,
                    "rank_display": r.rank_display,
                    "clear": r.clear,
                    "clear_display": r.clear_display,
                    "full_combo": r.full_combo,
                    "fc_display": r.fc_display,
                    "full_chain": r.full_chain,
                    "play_time": r.play_time or "",
                }
            )
        return result

    @staticmethod
    def _chu_diff_rows(song):
        rows = []
        for diff in song.difficulties:
            idx = diff.difficulty
            name = (
                CHU_DIFFICULTY_NAMES[idx]
                if idx < len(CHU_DIFFICULTY_NAMES)
                else f"LV{idx}"
            )
            css = name.lower().replace("'", "").replace(" ", "-")
            notes = (
                {
                    "tap": diff.notes.tap if diff.notes else 0,
                    "hold": diff.notes.hold if diff.notes else 0,
                    "slide": diff.notes.slide if diff.notes else 0,
                    "air": diff.notes.air if diff.notes else 0,
                    "flick": diff.notes.flick if diff.notes else 0,
                }
                if diff.notes
                else None
            )
            rows.append(
                {
                    "name": name,
                    "css_class": css,
                    "level": diff.level,
                    "level_value": diff.level_value,
                    "note_designer": diff.note_designer,
                    "notes": notes,
                }
            )
        return rows

    @staticmethod
    def _chu_bests_text(pi, bests) -> str:
        ls = [f"{pi.name}  Rating: {pi.rating:.2f}", "= Best 30 ="]
        for i, r in enumerate(bests.bests, 1):
            d = (
                CHU_DIFFICULTY_SHORT[r.level_index]
                if r.level_index < len(CHU_DIFFICULTY_SHORT)
                else "?"
            )
            ls.append(
                f"#{i} {r.song_name} [{d}{r.level}] {r.score} {r.rank_display} {r.clear_display}"
            )
        ls.append("= Selection 10 =")
        for i, r in enumerate(bests.selections, 1):
            d = (
                CHU_DIFFICULTY_SHORT[r.level_index]
                if r.level_index < len(CHU_DIFFICULTY_SHORT)
                else "?"
            )
            ls.append(
                f"#{i} {r.song_name} [{d}{r.level}] {r.score} {r.rank_display} {r.clear_display}"
            )
        ls.append("= New 20 =")
        for i, r in enumerate(bests.new_bests, 1):
            d = (
                CHU_DIFFICULTY_SHORT[r.level_index]
                if r.level_index < len(CHU_DIFFICULTY_SHORT)
                else "?"
            )
            ls.append(
                f"#{i} {r.song_name} [{d}{r.level}] {r.score} {r.rank_display} {r.clear_display}"
            )
        return "\n".join(ls)

    @staticmethod
    def _chu_recent_text(pi, recents) -> str:
        ls = [f"{pi.name}  好友码: {pi.friend_code}", "= Recent 50 ="]
        for i, r in enumerate(recents, 1):
            d = (
                CHU_DIFFICULTY_SHORT[r.level_index]
                if r.level_index < len(CHU_DIFFICULTY_SHORT)
                else "?"
            )
            ls.append(
                f"#{i} {r.song_name} [{d}{r.level}] {r.score} {r.rank_display} {r.clear_display}"
            )
        return "\n".join(ls)

    @staticmethod
    def _chu_song_text(song) -> str:
        ls = [
            f"{song.title}  [{song.genre}]",
            f"艺术家:{song.artist}  BPM:{song.bpm}  ID:{song.id}",
            "",
            "难度          等级  定数  谱师",
        ]
        for diff in song.difficulties:
            idx = diff.difficulty
            name = (
                CHU_DIFFICULTY_NAMES[idx]
                if idx < len(CHU_DIFFICULTY_NAMES)
                else f"LV{idx}"
            )
            lv = diff.level_value
            ls.append(
                f"  {name:<10} {diff.level:>4}  {lv:.1f}"
                if lv > 0
                else f"  {name:<10} {diff.level:>4}  -"
            )
        return "\n".join(ls)

    # --- /lxchu help ---

    async def help(self, ev: AstrMessageEvent):
        """显示中二节奏命令列表和当前授权模式。优先使用 HTML 模板渲染图片，无模板时回退纯文本。"""
        t = self._p._tmpl.get("chunithm_help")
        if t:
            if self._p._debug:
                logger.info("[lxdx] rendering help (Chunithm)")
            desc = (
                "已绑定开发者 API Key"
                if not self._p._is_oauth
                else "OAuth(PKCE) 交互授权"
            )
            url = await self._p.render_html(
                t,
                {
                    "plugin_display_name": "落雪DX (中二节奏)",
                    "plugin_version": self._p._version,
                    "auth_mode": "OAuth(PKCE)" if self._p._is_oauth else "api_key",
                    "auth_desc": desc,
                    "commands": [
                        {"name": "/lxchu bind <好友码>", "desc": "绑定玩家好友码"},
                        {
                            "name": "/lxchu bests <好友码>",
                            "desc": "Best 30 + Selection 10 + New 20",
                        },
                        {
                            "name": "/lxchu recent <好友码>",
                            "desc": "Recent 50 最近游玩",
                        },
                        {"name": "/lxchu song <名称/ID>", "desc": "查询歌曲信息"},
                        {
                            "name": "/lxchu login <授权码>",
                            "desc": "OAuth 授权登录",
                        },
                    ],
                },
                options=JINJA_OPTIONS,
            )
            yield ev.image_result(url)
        else:
            yield ev.plain_result(
                "/lxchu help|bind <好友码>|bests [fc]|recent [fc]|song <名称/ID>|login [<码>]"
            )

    # --- /lxchu bind ---

    async def bind(self, ev: AstrMessageEvent):
        """绑定中二节奏好友码：/lxchu bind <friend_code>。API Key 模式会验证好友码有效性。"""
        args = self._args(ev, 2)
        if not args:
            yield ev.plain_result("用法: /lxchu bind <friend_code>")
            return
        fc = args[0]
        uid = ev.get_sender_id()
        if not self._p._is_oauth:
            try:
                await self._p._chu_client.get_player_info(fc=int(fc))
            except Exception as e:
                yield ev.plain_result(f"绑定失败: {e}")
                return
        await self._p._st.kv_put(self._p._st.chu_binding_key(uid), fc)
        yield ev.plain_result(f"已绑定中二节奏好友码: {fc}")

    # --- /lxchu bests ---

    async def bests(self, ev: AstrMessageEvent):
        """查询中二节奏 Best 30 + Selection 10 + New 20：/lxchu bests [friend_code]。"""
        args = self._args(ev, 2)
        uid = ev.get_sender_id()
        if not self._p._is_oauth:
            fc_raw = (
                args[0]
                if args
                else await self._p._st.kv_get(self._p._st.chu_binding_key(uid))
            )
            if not fc_raw:
                yield ev.plain_result(
                    "请先 /lxchu bind <好友码> 或 /lxchu bests <好友码>"
                )
                return
            fc = int(fc_raw)
            try:
                bests = await self._p._chu_client.get_bests(fc=fc)
                pi = await self._p._chu_client.get_player_info(fc=fc)
            except LxnsError as e:
                yield ev.plain_result(str(e))
                return
        else:
            u = await self._p._restore_token(ev)
            if not u:
                yield ev.plain_result("请先 /lxchu login 授权")
                return
            try:
                bests = await self._p._chu_client.get_bests(uid=u)
                pi = await self._p._chu_client.get_player_info(uid=u)
            except AuthExpiredError as e:
                await self._p._st.kv_delete(self._p._st.token_key(u))
                self._p._auth.remove_tokens(u)
                yield ev.plain_result(str(e))
                return
            except LxnsError as e:
                yield ev.plain_result(str(e))
                return

        t = self._p._tmpl.get("chunithm_b50")
        if t:
            if self._p._debug:
                logger.info("[lxdx] rendering chunithm_b50")
            character_uri = await self._get_character_uri(pi)
            url = await self._p.render_html(
                t,
                {
                    "player_name": pi.name,
                    "rating": pi.rating,
                    "friend_code": pi.friend_code,
                    "character_uri": character_uri,
                    "bests": self._chu_score_rows(bests.bests),
                    "selections": self._chu_score_rows(bests.selections),
                    "new_bests": self._chu_score_rows(bests.new_bests),
                },
                options=JINJA_OPTIONS,
            )
            yield ev.image_result(url)
        else:
            yield ev.plain_result(self._chu_bests_text(pi, bests))

    # --- /lxchu recent ---

    async def recent(self, ev: AstrMessageEvent):
        """查询中二节奏 Recent 50：/lxchu recent [friend_code]。"""
        args = self._args(ev, 2)
        uid = ev.get_sender_id()
        if not self._p._is_oauth:
            fc_raw = (
                args[0]
                if args
                else await self._p._st.kv_get(self._p._st.chu_binding_key(uid))
            )
            if not fc_raw:
                yield ev.plain_result(
                    "请先 /lxchu bind <好友码> 或 /lxchu recent <好友码>"
                )
                return
            fc = int(fc_raw)
            try:
                recents = await self._p._chu_client.get_recents(fc=fc)
                pi = await self._p._chu_client.get_player_info(fc=fc)
            except LxnsError as e:
                yield ev.plain_result(str(e))
                return
        else:
            u = await self._p._restore_token(ev)
            if not u:
                yield ev.plain_result("请先 /lxchu login 授权")
                return
            try:
                recents = await self._p._chu_client.get_recents(uid=u)
                pi = await self._p._chu_client.get_player_info(uid=u)
            except AuthExpiredError as e:
                await self._p._st.kv_delete(self._p._st.token_key(u))
                self._p._auth.remove_tokens(u)
                yield ev.plain_result(str(e))
                return
            except LxnsError as e:
                yield ev.plain_result(str(e))
                return

        t = self._p._tmpl.get("chunithm_recent")
        if t:
            if self._p._debug:
                logger.info("[lxdx] rendering chunithm_recent")
            url = await self._p.render_html(
                t,
                {
                    "player_name": pi.name,
                    "friend_code": pi.friend_code,
                    "recent": self._chu_score_rows(recents),
                },
                options=JINJA_OPTIONS,
            )
            yield ev.image_result(url)
        else:
            yield ev.plain_result(self._chu_recent_text(pi, recents))

    # --- /lxchu song ---

    async def song(self, ev: AstrMessageEvent):
        """查询中二节奏歌曲信息：/lxchu song <名称 或 ID>。多个匹配时返回列表。"""
        args = self._args(ev, 2)
        if not args:
            yield ev.plain_result("用法: /lxchu song <名称 或 ID>")
            return
        q = " ".join(args)
        uid = ev.get_sender_id()
        if self._p._is_oauth:
            uid = await self._p._restore_token(ev) or ""
        res = await self._chu_lookup(q, uid)
        if not res:
            yield ev.plain_result(f"未找到: {q}")
            return
        if len(res) > 1:
            ns = "\n".join(f"  · {s.title} (ID:{s.id})" for s in res[:10])
            yield ev.plain_result(f"多个结果:\n{ns}")
            return
        s = res[0]
        try:
            fd = await self._p._chu_client.get_song(s.id)
            if fd:
                s = fd
        except Exception as e:
            logger.warning(f"[lxdx] failed to fetch detailed chunithm song: {e}")
        t = self._p._tmpl.get("chunithm_song_info")
        if t:
            if self._p._debug:
                logger.info(f"[lxdx] rendering chunithm_song_info for {s.title}")
            uri = await self._p._am.get_chunithm_jacket_data_uri(s.id) or ""
            url = await self._p.render_html(
                t,
                {
                    "song": {
                        "title": s.title,
                        "artist": s.artist,
                        "genre": s.genre,
                        "bpm": s.bpm,
                        "id": s.id,
                        "map": s.map,
                    },
                    "jacket_data_uri": uri,
                    "difficulties": self._chu_diff_rows(s),
                },
                options=JINJA_OPTIONS,
            )
            yield ev.image_result(url)
        else:
            yield ev.plain_result(self._chu_song_text(s))

    # --- /lxchu score ---

    async def score(self, ev: AstrMessageEvent):
        """查询单曲成绩：/lxchu score <歌曲名> [难度]。

        难度: 0-5 或 basic/advanced/expert/master/ultima/worldsend (可选，不指定时查询所有难度)
        """
        args = self._args(ev, 2)
        if not args:
            yield ev.plain_result(
                "用法: /lxchu score <歌曲名> [难度]\n难度: 0-5 或 basic/advanced/expert/master/ultima/worldsend (可选)"
            )
            return

        # 从后往前解析参数：最后一个可能是难度
        level_index = -1
        song_query_parts = args[:]

        # 检查最后一个参数是否是难度
        if args[-1].lower() in CHU_DIFF_MAP:
            level_index = CHU_DIFF_MAP[args[-1].lower()]
            song_query_parts = args[:-1]

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
                yield ev.plain_result("请先登录: /lxchu login")
                return

        res = await self._chu_lookup(q, uid)
        if not res:
            yield ev.plain_result(f"未找到歌曲: {q}")
            return
        if len(res) > 1:
            ns = "\n".join(f"  · {s.title} (ID:{s.id})" for s in res[:10])
            yield ev.plain_result(f"多个结果，请使用ID查询:\n{ns}")
            return

        song = res[0]

        # 获取好友码（仅API Key模式需要）
        fc = 0
        if not self._p._is_oauth:
            fc_str = await self._p._st.kv_get(self._p._st.chu_binding_key(uid))
            if not fc_str:
                yield ev.plain_result("请先绑定好友码: /lxchu bind <好友码>")
                return
            try:
                fc = int(fc_str)
            except ValueError:
                yield ev.plain_result("好友码格式错误，请重新绑定")
                return

        # 如果未指定难度，查询所有难度
        if level_index == -1:
            try:
                found_scores = await self._chu_query_all_difficulties(song, fc, uid)
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

            # 获取玩家信息及角色立绘
            character_uri = ""
            try:
                pi = await self._p._chu_client.get_player_info(fc=fc, uid=uid)
                if self._p._debug:
                    logger.info(
                        f"[lxdx] Chunithm player info fetched: name={pi.name if pi else 'None'}, rating={pi.rating if pi else 0}"
                    )
                character_uri = await self._get_character_uri(pi)
            except Exception as e:
                logger.warning(f"[lxdx] failed to fetch Chunithm player info: {e}")
                pi = None

            # 渲染多难度模板
            t = self._p._tmpl.get("chunithm_song_score")
            if t:
                if self._p._debug:
                    logger.info(
                        f"[lxdx] rendering chunithm_song_score for {song.title} (multiple difficulties)"
                    )
                uri = await self._p._am.get_chunithm_jacket_data_uri(song.id) or ""

                # 构建scores数组
                scores_data = []
                for idx, score in found_scores:
                    scores_data.append(
                        {
                            "difficulty_name": CHU_DIFFICULTY_NAMES[idx],
                            "difficulty_class": CHU_DIFFICULTY_NAMES[idx]
                            .lower()
                            .replace("'", "")
                            .replace(" ", "-"),
                            "level": score.level,
                            "score": score.score,
                            "rating": score.rating,
                            "over_power": score.over_power,
                            "rank": score.rank,
                            "clear": score.clear,
                            "clear_display": score.clear_display,
                            "full_combo": score.full_combo,
                            "fc_display": score.fc_display,
                            "full_chain": score.full_chain,
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
                            "rating": pi.rating if pi else 0.0,
                        },
                        "scores": scores_data,
                        "jacket_data_uri": uri,
                        "character_uri": character_uri,
                    },
                    options=JINJA_OPTIONS,
                )
                yield ev.image_result(url)
            else:
                # 纯文本输出
                lines = [f"{song.title} 成绩:"]
                for idx, score in found_scores:
                    diff_name = CHU_DIFFICULTY_NAMES[idx]
                    lines.append(
                        f"{diff_name} {score.level}: {score.score} ({score.rank_display})"
                    )
                yield ev.plain_result("\n".join(lines))
            return

        # 查询指定难度的成绩
        try:
            score = await self._p._chu_client.get_player_best(
                song_id=song.id,
                level_index=level_index,
                fc=fc,
                uid=uid,
            )
            if not score:
                yield ev.plain_result(
                    f"未找到成绩: {song.title} {CHU_DIFFICULTY_NAMES[level_index]}"
                )
                return

            # 获取玩家信息及角色立绘
            character_uri = ""
            try:
                pi = await self._p._chu_client.get_player_info(fc=fc, uid=uid)
                if self._p._debug:
                    logger.info(
                        f"[lxdx] Chunithm player info fetched: name={pi.name if pi else 'None'}, rating={pi.rating if pi else 0}"
                    )
                character_uri = await self._get_character_uri(pi)
            except Exception as e:
                logger.warning(f"[lxdx] failed to fetch Chunithm player info: {e}")
                pi = None

            # 渲染模板
            t = self._p._tmpl.get("chunithm_song_score")
            if t:
                if self._p._debug:
                    logger.info(
                        f"[lxdx] rendering chunithm_song_score for {song.title}"
                    )
                uri = await self._p._am.get_chunithm_jacket_data_uri(song.id) or ""
                url = await self._p.render_html(
                    t,
                    {
                        "song": {
                            "title": song.title,
                        },
                        "player": {
                            "name": pi.name if pi else "Unknown",
                            "rating": pi.rating if pi else 0.0,
                        },
                        "scores": [
                            {
                                "difficulty_name": CHU_DIFFICULTY_NAMES[level_index],
                                "difficulty_class": CHU_DIFFICULTY_NAMES[level_index]
                                .lower()
                                .replace("'", "")
                                .replace(" ", "-"),
                                "level": score.level,
                                "score": score.score,
                                "rating": score.rating,
                                "over_power": score.over_power,
                                "rank": score.rank,
                                "clear": score.clear,
                                "clear_display": score.clear_display,
                                "full_combo": score.full_combo,
                                "fc_display": score.fc_display,
                                "full_chain": score.full_chain,
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
                yield ev.plain_result(self._chu_score_text(song, score, level_index))
        except LxnsError as e:
            yield ev.plain_result(f"查询失败: {e}")
        except Exception as e:
            logger.error(f"[lxdx] Chunithm score query error: {e}")
            yield ev.plain_result("查询过程中发生错误")

    # --- helpers ---

    @staticmethod
    def _chu_score_text(song, score, level_index) -> str:
        """生成单曲成绩的纯文本输出。"""
        diff_name = (
            CHU_DIFFICULTY_NAMES[level_index]
            if level_index < len(CHU_DIFFICULTY_NAMES)
            else f"LV{level_index}"
        )

        ls = [
            f"{song.title}",
            f"{diff_name} {score.level}",
            "",
            f"分数: {score.score}",
            f"评级: {score.rank_display}",
            f"Rating: {score.rating:.2f}",
            f"Over Power: {score.over_power:.2f}",
            f"通关类型: {score.clear_display if score.clear else '-'}",
            f"FULL COMBO: {score.fc_display if score.full_combo else '-'}",
            f"FULL CHAIN: {score.full_chain.upper() if score.full_chain else '-'}",
            f"游玩时间: {score.play_time if score.play_time else '-'}",
        ]
        return "\n".join(ls)
