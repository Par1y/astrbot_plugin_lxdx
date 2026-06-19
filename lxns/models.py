"""LXNS API 数据模型和自定义异常。

数据模型均为 dataclass，直接从 API JSON 反序列化而来。
异常层级：LxnsError(基础) → AuthExpiredError / AuthRequiredError / ApiRequestError
"""

from dataclasses import dataclass, field
from typing import Optional
from enum import IntEnum, StrEnum

JINJA_OPTIONS = {
    "type": "png",
    "full_page": True,
    "scale": "css",
}


class DifficultyIndex(IntEnum):
    """难度索引：0=BASIC, 1=ADVANCED, 2=EXPERT, 3=MASTER, 4=Re:MASTER"""

    BASIC = 0
    ADVANCED = 1
    EXPERT = 2
    MASTER = 3
    RE_MASTER = 4


# 难度显示常量（用于模板生成和纯文本渲染）
DIFFICULTY_NAMES = ["BASIC", "ADVANCED", "EXPERT", "MASTER", "Re:MASTER"]  # 全称
DIFFICULTY_COLORS = {
    "BASIC": "#22bb5b",
    "ADVANCED": "#f6ba31",
    "EXPERT": "#f35871",
    "MASTER": "#9a69e7",
    "Re:MASTER": "#ba67f9",
}  # 对应 HTML 颜色
DIFFICULTY_SHORT = ["Bas", "Adv", "Exp", "Mas", "ReM"]  # 缩写（3 字母）


# --- 歌曲 & 成绩 ---


@dataclass
class SongInfo:
    """歌曲信息。difficulty_details 包含每个难度的完整元数据。"""

    id: int
    title: str
    artist: str
    genre: str
    bpm: int
    version: int
    is_utage: bool
    map: str = ""
    difficulty_details: list[dict] = field(default_factory=list)
    image_url: Optional[str] = None
    aliases: list[str] = field(default_factory=list)
    rights: str = ""

    @property
    def display_id(self) -> int:
        """显示用 ID：DX 曲 ID > 10000 取模显示，宴曲 ID 保留原始值。"""
        if self.is_utage:
            return self.id
        if self.id > 100000:
            return self.id
        if self.id > 10000:
            return self.id % 10000
        return self.id


@dataclass
class PlayerRecord:
    """单曲成绩。achievement_pct 属性自动将万分率（0-10100）转换为百分率（0-101%）。"""

    song_id: int
    level_index: int
    title: str
    difficulty: str
    level_value: float  # 定数
    achievement: float  # 原始达成率（可能是万分率 0~10100，也可能是百分率 0~101）
    dx_score: int
    dx_rating: int
    fc: str = ""
    fs: str = ""
    rate: str = ""
    combo_status: str = ""
    sync_status: str = ""
    play_time: Optional[str] = None

    @property
    def achievement_pct(self) -> float:
        """将 achievement 统一转换为百分率：>100 视为万分率除以 10000，否则直接返回。"""
        return (
            self.achievement / 10000.0 if self.achievement > 100 else self.achievement
        )

    @property
    def rank_display(self) -> str:
        """根据达成率返回评级显示（D ~ AP+）。"""
        a = self.achievement_pct
        if a >= 100.5:
            return "AP+"
        if a >= 100.0:
            return "AP"
        if a >= 99.5:
            return "SSS+"
        if a >= 99.0:
            return "SSS"
        if a >= 98.0:
            return "SS+"
        if a >= 97.0:
            return "SS"
        if a >= 94.0:
            return "S+"
        if a >= 90.0:
            return "S"
        if a >= 80.0:
            return "AAA"
        if a >= 70.0:
            return "AA"
        if a >= 60.0:
            return "A"
        if a >= 50.0:
            return "B"
        if a >= 40.0:
            return "C"
        return "D"


# --- 认证 ---


@dataclass
class TokenInfo:
    """OAuth Token。expires_at 为 Unix 时间戳（生成时的绝对时间）。"""

    access_token: str
    refresh_token: str
    expires_at: float


@dataclass
class UserProfile:
    """LXNS 用户资料（通过 /api/v0/user/profile 获取）。id 作为 OAuth 模式下的用户主键。"""

    id: int
    name: str
    email: str = ""
    avatar: str = ""


@dataclass
class PlayerInfo:
    """玩家基本信息。"""

    name: str
    rating: int
    friend_code: str
    class_rank: int
    course_rank: int = 0
    star: int = 0
    trophy: Optional[dict] = None
    icon: Optional[dict] = None
    upload_time: Optional[str] = None


@dataclass
class SongRecord:
    """单曲成绩记录列表（含可选歌曲信息，当前未使用）。"""

    records: list[PlayerRecord] = field(default_factory=list)
    song: Optional[SongInfo] = None


@dataclass
class Score:
    """单曲成绩（用于查询玩家在特定谱面的最佳成绩）。"""

    id: int
    song_name: str = ""
    level: str = ""
    level_index: int = 0
    achievements: float = 0.0
    fc: str = ""
    fs: str = ""
    dx_score: int = 0
    dx_star: int = 0
    dx_rating: float = 0.0
    rate: str = ""
    type: str = ""
    play_time: Optional[str] = None
    upload_time: Optional[str] = None
    last_played_time: Optional[str] = None

    @property
    def achievement_pct(self) -> float:
        """将 achievements 统一转换为百分率。"""
        return (
            self.achievements / 100.0 if self.achievements > 100 else self.achievements
        )

    @property
    def rank_display(self) -> str:
        """根据达成率返回评级显示（D ~ AP+）。"""
        a = self.achievement_pct
        if a >= 100.5:
            return "AP+"
        if a >= 100.0:
            return "AP"
        if a >= 99.5:
            return "SSS+"
        if a >= 99.0:
            return "SSS"
        if a >= 98.0:
            return "SS+"
        if a >= 97.0:
            return "SS"
        if a >= 94.0:
            return "S+"
        if a >= 90.0:
            return "S"
        if a >= 80.0:
            return "AAA"
        if a >= 70.0:
            return "AA"
        if a >= 60.0:
            return "A"
        if a >= 50.0:
            return "B"
        if a >= 40.0:
            return "C"
        return "D"


@dataclass
class PlayerB50:
    """玩家 Best 50 数据：standard 为旧版本 Best 35，dx 为现版本 Best 15。"""

    standard_total: int
    dx_total: int
    standard: list[Score]
    dx: list[Score]


# --- 自定义异常 ---


class LxnsError(Exception):
    """LXNS 基础异常。message 为面向用户的中文描述。"""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)

    def __str__(self):
        return self.message


class AuthExpiredError(LxnsError):
    """Token 过期（需重新登录）。"""


class AuthRequiredError(LxnsError):
    """未登录（需先执行 OAuth 授权）。"""


class ApiRequestError(LxnsError):
    """API 请求失败（网络/服务端错误）。"""


# ============================================================
# Chunithm (中二节奏) 枚举与常量
# ============================================================


class ChuLevelIndex(IntEnum):
    BASIC = 0
    ADVANCED = 1
    EXPERT = 2
    MASTER = 3
    ULTIMA = 4
    WORLDS_END = 5


CHU_DIFFICULTY_NAMES = [
    "BASIC",
    "ADVANCED",
    "EXPERT",
    "MASTER",
    "ULTIMA",
    "WORLD'S END",
]
CHU_DIFFICULTY_COLORS = {
    "BASIC": "#22bb5b",
    "ADVANCED": "#f6ba31",
    "EXPERT": "#f35871",
    "MASTER": "#9a69e7",
    "ULTIMA": "#ba67f9",
    "WORLD'S END": "#e040fb",
}
CHU_DIFFICULTY_SHORT = ["Bas", "Adv", "Exp", "Mas", "Ult", "WE"]


class ChuClearType(StrEnum):
    CATASTROPHY = "catastrophy"
    ABSOLUTE = "absolute"
    BRAVE = "brave"
    HARD = "hard"
    CLEAR = "clear"
    FAILED = "failed"


class ChuFullComboType(StrEnum):
    AJC = "alljusticecritical"
    ALL_JUSTICE = "alljustice"
    FULL_COMBO = "fullcombo"


class ChuFullChainType(StrEnum):
    FULL_CHAIN = "fullchain"
    FULL_CHAIN2 = "fullchain2"


class ChuRankType(StrEnum):
    SSSP = "sssp"
    SSS = "sss"
    SSP = "ssp"
    SS = "ss"
    SP = "sp"
    S = "s"
    AAA = "aaa"
    AA = "aa"
    A = "a"
    BBB = "bbb"
    BB = "bb"
    B = "b"
    C = "c"
    D = "d"


_CHU_RANK_ORDER = {
    ChuRankType.SSSP: 0,
    ChuRankType.SSS: 1,
    ChuRankType.SSP: 2,
    ChuRankType.SS: 3,
    ChuRankType.SP: 4,
    ChuRankType.S: 5,
    ChuRankType.AAA: 6,
    ChuRankType.AA: 7,
    ChuRankType.A: 8,
    ChuRankType.BBB: 9,
    ChuRankType.BB: 10,
    ChuRankType.B: 11,
    ChuRankType.C: 12,
    ChuRankType.D: 13,
}

_CHU_CLEAR_DISPLAY = {
    "catastrophy": "CATASTROPHY",
    "absolute": "ABSOLUTE",
    "brave": "BRAVE",
    "hard": "HARD",
    "clear": "CLEAR",
    "failed": "FAILED",
}

_CHU_FC_DISPLAY = {
    "alljusticecritical": "AJC",
    "alljustice": "ALL JUSTICE",
    "fullcombo": "FULL COMBO",
}


# ============================================================
# Chunithm (中二节奏) 数据模型
# ============================================================


@dataclass
class ChuNotes:
    total: int = 0
    tap: int = 0
    hold: int = 0
    slide: int = 0
    air: int = 0
    flick: int = 0


@dataclass
class ChuSongDifficulty:
    difficulty: int = 0
    level: str = ""
    level_value: float = 0.0
    note_designer: str = ""
    version: int = 0
    notes: Optional[ChuNotes] = None
    origin_id: int = 0
    kanji: str = ""
    star: int = 0


@dataclass
class ChuSongInfo:
    id: int
    title: str
    artist: str
    genre: str
    bpm: int
    version: int
    difficulties: list[ChuSongDifficulty] = field(default_factory=list)
    map: str = ""
    rights: str = ""
    locked: bool = False
    disabled: bool = False
    aliases: list[str] = field(default_factory=list)


@dataclass
class ChuScore:
    id: int
    score: int
    rating: float = 0.0
    over_power: float = 0.0
    level_index: int = 0
    song_name: str = ""
    level: str = ""
    clear: str = ""
    full_combo: str = ""
    full_chain: str = ""
    rank: str = ""
    play_time: Optional[str] = None
    upload_time: Optional[str] = None
    last_played_time: Optional[str] = None

    @property
    def clear_display(self) -> str:
        return _CHU_CLEAR_DISPLAY.get(
            self.clear, self.clear.upper() if self.clear else ""
        )

    @property
    def fc_display(self) -> str:
        return _CHU_FC_DISPLAY.get(
            self.full_combo, self.full_combo.upper() if self.full_combo else ""
        )

    @property
    def rank_display(self) -> str:
        m = {
            "sssp": "SSS+",
            "sss": "SSS",
            "ssp": "SS+",
            "ss": "SS",
            "sp": "S+",
            "s": "S",
            "aaa": "AAA",
            "aa": "AA",
            "a": "A",
            "bbb": "BBB",
            "bb": "BB",
            "b": "B",
            "c": "C",
            "d": "D",
        }
        return m.get(self.rank, self.rank.upper() if self.rank else "")


@dataclass
class ChuPlayerBests:
    bests: list[ChuScore] = field(default_factory=list)
    selections: list[ChuScore] = field(default_factory=list)
    new_bests: list[ChuScore] = field(default_factory=list)


@dataclass
class ChuPlayerInfo:
    name: str = ""
    level: int = 0
    rating: float = 0.0
    rating_possession: str = ""
    friend_code: int = 0
    class_emblem: dict = field(default_factory=lambda: {"base": 0, "medal": 0})
    reborn_count: int = 0
    over_power: float = 0.0
    over_power_progress: float = 0.0
    currency: int = 0
    total_currency: int = 0
    total_play_count: int = 0
    trophy: Optional[dict] = None
    character: Optional[dict] = None
    name_plate: Optional[dict] = None
    map_icon: Optional[dict] = None
    upload_time: Optional[str] = None


@dataclass
class ChuGenre:
    id: int = 0
    genre: str = ""


@dataclass
class ChuVersion:
    id: int = 0
    title: str = ""
    version: int = 0


@dataclass
class ChuSongListResult:
    songs: list[ChuSongInfo] = field(default_factory=list)
    genres: list[ChuGenre] = field(default_factory=list)
    versions: list[ChuVersion] = field(default_factory=list)


@dataclass
class ChuAlias:
    song_id: int = 0
    aliases: list[str] = field(default_factory=list)


@dataclass
class ChuRatingTrend:
    rating: float = 0.0
    bests_rating: float = 0.0
    selections_rating: float = 0.0
    recents_rating: float = 0.0
    new_bests_rating: float = 0.0
    date: str = ""
