"""LXNS OAuth PKCE 授权模块。

PKCE (Proof Key for Code Exchange) 流程：
1. generate_pkce() 生成 code_verifier + code_challenge + state
2. build_authorize_url() 构建跳转授权 URL
3. 用户授权后服务器回调，exchange_code() 用 code + verifier 换 Token
4. refresh_token() 定期刷新过期 Token
"""

import hashlib
import os
import base64
import time
import urllib.parse
from dataclasses import dataclass

from .models import TokenInfo


@dataclass
class PKCEParams:
    """PKCE 参数：code_verifier（原始随机串）、code_challenge（S256 哈希）、state（随机请求标识）。

    本插件使用 OOB（urn:ietf:wg:oauth:2.0:oob）流程，用户手动粘贴授权码，无服务端回调，
    state 无法回传校验，因此不提供 CSRF 防护；授权码与发起会话的绑定由 PKCE code_verifier
    （按 uid 缓存、交换时必须匹配）保证。state 仅作为随授权 URL 携带的请求标识保留。
    """

    code_verifier: str
    code_challenge: str
    state: str


class LxnsAuth:
    """OAuth 授权管理：PKCE 生成、授权 URL 构建、Token 内存缓存。"""

    AUTHORIZE_URL = "https://maimai.lxns.net/oauth/authorize"
    TOKEN_URL = "https://maimai.lxns.net/api/v0/oauth/token"

    def __init__(self, client_id: str):
        """client_id: 落雪 OAuth 应用的客户端 ID。"""
        self.client_id = client_id
        self._tokens: dict[str, TokenInfo] = {}  # uid → TokenInfo 内存缓存

    @staticmethod
    def generate_pkce() -> PKCEParams:
        """生成 PKCE 参数。

        - code_verifier: 64 字节随机 → url-safe base64 无填充
        - code_challenge: SHA256(code_verifier) → url-safe base64 无填充
        - state: 32 字节随机 → url-safe base64 无填充（请求标识；OOB 流程无回调，不参与校验）
        """
        v = os.urandom(64)
        cv = base64.urlsafe_b64encode(v).rstrip(b"=").decode("ascii")
        d = hashlib.sha256(cv.encode("ascii")).digest()
        cc = base64.urlsafe_b64encode(d).rstrip(b"=").decode("ascii")
        s = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode("ascii")
        return PKCEParams(code_verifier=cv, code_challenge=cc, state=s)

    def build_authorize_url(
        self, pkce: PKCEParams, redirect_uri: str, scope: str = ""
    ) -> str:
        """构建 OAuth 授权跳转 URL。redirect_uri 必填，scope 可选（如 "read_player read_user_profile"）。"""
        p = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "state": pkce.state,
            "code_challenge": pkce.code_challenge,
            "code_challenge_method": "S256",
        }
        if scope:
            p["scope"] = scope
        return f"{self.AUTHORIZE_URL}?{urllib.parse.urlencode(p)}"

    # Token 内存缓存管理（KV 持久化由 StorageManager 负责）
    def store_tokens(self, uid: str, t: TokenInfo) -> None:
        self._tokens[uid] = t

    def get_tokens(self, uid: str) -> TokenInfo | None:
        return self._tokens.get(uid)

    def remove_tokens(self, uid: str) -> None:
        self._tokens.pop(uid, None)

    @staticmethod
    def is_token_expired(t: TokenInfo) -> bool:
        """检查 Token 是否已过期（提前 30 秒视为过期，留出刷新余量）。"""
        return time.time() >= t.expires_at - 30

    @staticmethod
    def make_token_info(
        access_token: str, refresh_token: str, expires_in: int
    ) -> TokenInfo:
        """根据服务器返回的令牌构建 TokenInfo（计算 expires_at 绝对时间戳）。"""
        return TokenInfo(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=time.time() + expires_in,
        )
