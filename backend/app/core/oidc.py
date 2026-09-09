"""OIDC 授权码 + PKCE 客户端（不依赖具体 IdP，走 discovery）。"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt

from ..config import settings

log = logging.getLogger(__name__)

_STATE_TTL = 600  # 授权请求有效期（秒）


@dataclass
class _PendingAuth:
    code_verifier: str
    nonce: str
    redirect_after: str
    created_at: float = field(default_factory=time.time)


class OIDCClient:
    """缓存 discovery 文档与 JWKS，串起 authorize → callback 流程。"""

    def __init__(self) -> None:
        self._meta: dict[str, Any] | None = None
        self._meta_fetched_at: float = 0.0
        self._jwks: dict[str, Any] | None = None
        self._jwks_fetched_at: float = 0.0
        self._pending: dict[str, _PendingAuth] = {}

    # ---------------- discovery ----------------
    async def metadata(self) -> dict[str, Any]:
        if self._meta and time.time() - self._meta_fetched_at < 3600:
            return self._meta
        issuer = settings.oidc_issuer.rstrip("/")
        url = f"{issuer}/.well-known/openid-configuration"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            self._meta = resp.json()
        self._meta_fetched_at = time.time()
        return self._meta

    async def _jwks_keys(self) -> dict[str, Any]:
        if self._jwks and time.time() - self._jwks_fetched_at < 3600:
            return self._jwks
        meta = await self.metadata()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(meta["jwks_uri"])
            resp.raise_for_status()
            self._jwks = resp.json()
        self._jwks_fetched_at = time.time()
        return self._jwks

    # ---------------- 授权 ----------------
    def _gc_pending(self) -> None:
        now = time.time()
        stale = [k for k, v in self._pending.items() if now - v.created_at > _STATE_TTL]
        for k in stale:
            self._pending.pop(k, None)

    async def build_authorize_url(self, redirect_after: str = "/") -> str:
        meta = await self.metadata()
        self._gc_pending()

        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(16)
        verifier = secrets.token_urlsafe(64)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        )
        self._pending[state] = _PendingAuth(verifier, nonce, redirect_after)

        params = {
            "response_type": "code",
            "client_id": settings.oidc_client_id,
            "redirect_uri": settings.oidc_redirect_uri,
            "scope": settings.oidc_scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return str(httpx.URL(meta["authorization_endpoint"]).copy_merge_params(params))

    async def exchange_code(self, code: str, state: str) -> tuple[dict[str, Any], str]:
        """用授权码换取用户 claims，返回 (claims, 登录后跳转路径)。"""
        pending = self._pending.pop(state, None)
        if pending is None:
            raise ValueError("state 无效或已过期，请重新发起登录")
        if time.time() - pending.created_at > _STATE_TTL:
            raise ValueError("授权请求超时，请重新登录")

        meta = await self.metadata()
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.oidc_redirect_uri,
            "client_id": settings.oidc_client_id,
            "code_verifier": pending.code_verifier,
        }
        auth = None
        # 优先用 client_secret_post；IdP 只支持 basic 时由 httpx auth 兜底
        if settings.oidc_client_secret:
            data["client_secret"] = settings.oidc_client_secret
            auth = (settings.oidc_client_id, settings.oidc_client_secret)

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(meta["token_endpoint"], data=data)
            if resp.status_code >= 400 and auth:
                resp = await client.post(
                    meta["token_endpoint"],
                    data={k: v for k, v in data.items() if k != "client_secret"},
                    auth=auth,
                )
            if resp.status_code >= 400:
                raise ValueError(f"换取令牌失败: {resp.status_code} {resp.text[:300]}")
            token = resp.json()

        claims = await self._verify_id_token(token.get("id_token"), pending.nonce)

        # 有些 IdP 的 id_token 精简，补一次 userinfo
        if meta.get("userinfo_endpoint") and token.get("access_token"):
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    ur = await client.get(
                        meta["userinfo_endpoint"],
                        headers={"Authorization": f"Bearer {token['access_token']}"},
                    )
                if ur.status_code < 400:
                    userinfo = ur.json()
                    claims = {**userinfo, **claims}
            except httpx.HTTPError as exc:  # userinfo 失败不阻断登录
                log.warning("userinfo 获取失败: %s", exc)

        return claims, pending.redirect_after

    async def _verify_id_token(self, id_token: str | None, nonce: str) -> dict[str, Any]:
        if not id_token:
            raise ValueError("IdP 未返回 id_token")
        jwks = await self._jwks_keys()
        header = jwt.get_unverified_header(id_token)
        kid = header.get("kid")
        key_data = next(
            (k for k in jwks.get("keys", []) if kid is None or k.get("kid") == kid), None
        )
        if key_data is None:
            # 密钥轮换：强制刷新一次 JWKS 再试
            self._jwks_fetched_at = 0.0
            jwks = await self._jwks_keys()
            key_data = next(
                (k for k in jwks.get("keys", []) if kid is None or k.get("kid") == kid), None
            )
        if key_data is None:
            raise ValueError("找不到匹配的 JWKS 公钥")

        meta = await self.metadata()
        claims = jwt.decode(
            id_token,
            key=jwt.PyJWK(key_data).key,
            algorithms=[header.get("alg", "RS256")],
            audience=settings.oidc_client_id,
            issuer=meta.get("issuer", settings.oidc_issuer),
            options={"require": ["exp", "iat", "sub"]},
        )
        if claims.get("nonce") and claims["nonce"] != nonce:
            raise ValueError("nonce 校验失败")
        return claims

    async def end_session_url(self, post_logout_redirect: str) -> str | None:
        try:
            meta = await self.metadata()
        except httpx.HTTPError:
            return None
        endpoint = meta.get("end_session_endpoint")
        if not endpoint:
            return None
        return str(
            httpx.URL(endpoint).copy_merge_params(
                {"post_logout_redirect_uri": post_logout_redirect, "client_id": settings.oidc_client_id}
            )
        )


oidc_client = OIDCClient()
