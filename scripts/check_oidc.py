#!/usr/bin/env python
"""OIDC 配置自检：在打开浏览器之前就把常见问题查出来。

用法（在项目根目录）：
    backend/.venv/bin/python scripts/check_oidc.py

依次检查：discovery 可达性、协议能力、client_id/secret、
redirect_uri 是否已在 IdP 注册、scope 与管理员组是否配套。
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import httpx  # noqa: E402

from app.config import settings  # noqa: E402

OK, WARN, BAD = "  \033[32m✓\033[0m", "  \033[33m!\033[0m", "  \033[31m✗\033[0m"
problems: list[str] = []


def bad(msg: str, hint: str = "") -> None:
    print(f"{BAD} {msg}")
    if hint:
        print(f"      → {hint}")
    problems.append(msg)


def main() -> int:
    print("\n=== 1. 基本配置 ===")
    if not settings.oidc_enabled:
        print(f"{WARN} OIDC_ENABLED=false（下面的检查照常进行，但登录页不会显示 SSO 按钮）")
    else:
        print(f"{OK} OIDC_ENABLED=true")

    if not settings.oidc_issuer:
        bad("OIDC_ISSUER 为空", "填 IdP 的根地址，例 https://auth.example.com")
        return report()
    print(f"{OK} OIDC_ISSUER = {settings.oidc_issuer}")

    if not settings.oidc_client_id:
        bad("OIDC_CLIENT_ID 为空", "在 IdP 里创建 OIDC 客户端后把 Client ID 填进 .env")
    else:
        print(f"{OK} OIDC_CLIENT_ID = {settings.oidc_client_id}")

    if not settings.oidc_client_secret:
        print(f"{WARN} OIDC_CLIENT_SECRET 为空 —— 仅当 IdP 把该客户端设为 public(PKCE only) 时才正确")
    else:
        print(f"{OK} OIDC_CLIENT_SECRET 已设置（{len(settings.oidc_client_secret)} 字符）")

    print("\n=== 2. discovery 文档 ===")
    url = settings.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration"
    try:
        resp = httpx.get(url, timeout=15, follow_redirects=True)
        resp.raise_for_status()
        meta = resp.json()
    except httpx.HTTPError as exc:
        bad(f"无法获取 discovery: {exc}", f"确认后端这台机器能访问 {url}")
        return report()
    print(f"{OK} {url} 可达")

    declared = meta.get("issuer", "")
    if declared.rstrip("/") != settings.oidc_issuer.rstrip("/"):
        bad(
            f"issuer 不一致：配置的是 {settings.oidc_issuer}，IdP 声明的是 {declared}",
            f"把 OIDC_ISSUER 改成 {declared} —— ID Token 校验会比对这个值，不一致必然失败",
        )
    else:
        print(f"{OK} issuer 与 IdP 声明一致")

    for name, key in (
        ("authorization_endpoint", "authorization_endpoint"),
        ("token_endpoint", "token_endpoint"),
        ("jwks_uri", "jwks_uri"),
    ):
        if meta.get(key):
            print(f"{OK} {name}: {meta[key]}")
        else:
            bad(f"discovery 缺少 {name}")

    print("\n=== 3. 协议能力 ===")
    pkce = meta.get("code_challenge_methods_supported") or []
    if "S256" in pkce:
        print(f"{OK} 支持 PKCE S256")
    else:
        bad(f"IdP 不支持 PKCE S256（声明的是 {pkce}）", "本平台强制使用 PKCE，需要 IdP 支持")

    if "code" in (meta.get("response_types_supported") or ["code"]):
        print(f"{OK} 支持授权码模式")
    else:
        bad("IdP 不支持 response_type=code")

    algs = meta.get("id_token_signing_alg_values_supported") or []
    if algs and not ({"RS256", "ES256", "RS512", "ES384", "PS256"} & set(algs)):
        print(f"{WARN} ID Token 签名算法为 {algs}，请确认 PyJWT 能处理")
    elif algs:
        print(f"{OK} ID Token 签名算法: {', '.join(algs)}")

    print("\n=== 4. scope 与 claim ===")
    scopes = settings.oidc_scopes.split()
    supported = meta.get("scopes_supported")
    if "openid" not in scopes:
        bad("OIDC_SCOPES 必须包含 openid")
    for s in scopes:
        if supported and s not in supported:
            print(f"{WARN} scope `{s}` 不在 IdP 声明的 {supported} 中")
    print(f"{OK} 请求的 scope: {settings.oidc_scopes}")

    if settings.oidc_admin_group:
        print(f"{OK} 管理员组: {settings.oidc_admin_group}（claim: {settings.oidc_groups_claim}）")
        if supported and "groups" in supported and "groups" not in scopes:
            bad(
                "配了 OIDC_ADMIN_GROUP 但 OIDC_SCOPES 里没有 groups",
                "IdP 只在请求了 groups scope 时才会下发该 claim，"
                "否则组信息拿不到、管理员永远同步不上",
            )
    else:
        print(f"{WARN} 未配 OIDC_ADMIN_GROUP —— 角色不会跟 IdP 同步，需手工在后台设置管理员")

    print("\n=== 5. redirect_uri 是否已在 IdP 注册 ===")
    print(f"     待验证: {settings.oidc_redirect_uri}")
    if not settings.oidc_client_id:
        print(f"{WARN} 缺少 client_id，跳过此项")
        return report()

    params = {
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": settings.oidc_redirect_uri,
        "scope": settings.oidc_scopes,
        "state": "preflight-check",
        "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        "code_challenge_method": "S256",
    }
    try:
        r = httpx.get(meta["authorization_endpoint"], params=params, timeout=15,
                      follow_redirects=False)
    except httpx.HTTPError as exc:
        bad(f"访问授权端点失败: {exc}")
        return report()

    body = r.text[:400]
    location = r.headers.get("location", "")
    err = ""
    if location:
        err = (parse_qs(urlparse(location).query).get("error") or [""])[0]

    if err in {"invalid_redirect_uri", "invalid_request"} or "redirect_uri" in body.lower():
        bad(
            f"IdP 拒绝了这个 redirect_uri（HTTP {r.status_code} {err}）",
            f"在 IdP 的客户端设置里把 {settings.oidc_redirect_uri} 加入回调白名单，必须完全一致",
        )
    elif err in {"unauthorized_client", "invalid_client"} or "invalid_client" in body.lower():
        bad(f"IdP 不认这个 client_id（{err}）", "确认 OIDC_CLIENT_ID 与 IdP 中的客户端一致")
    elif r.status_code in (200, 302, 303):
        print(f"{OK} 授权端点接受了该 client_id + redirect_uri（HTTP {r.status_code}）")
    else:
        print(f"{WARN} 授权端点返回 HTTP {r.status_code}，需人工确认：{body[:200]}")

    return report()


def report() -> int:
    print()
    if problems:
        print(f"\033[31m发现 {len(problems)} 个问题，逐条修完再试登录。\033[0m\n")
        return 1
    print("\033[32m配置检查通过，可以在登录页点 SSO 按钮试了。\033[0m\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
