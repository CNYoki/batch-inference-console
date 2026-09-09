"""认证：本地账号密码 + OIDC 单点登录（双模式并存）。"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from ..config import settings
from ..core.deps import DB, CurrentUser
from ..core.oidc import oidc_client
from ..core.security import create_session_token, hash_password, verify_password
from ..models import AuditLog, AuthSource, User, UserRole
from ..schemas import AuthInfo, ChangePasswordRequest, LoginRequest, MyPreferences, UserOut
from ..services.users import would_orphan_admin

log = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


def _set_session_cookie(response: Response, user: User) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=create_session_token(user.id, user.role.value),
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,  # type: ignore[arg-type]
        path="/",
    )


async def _audit(db: DB, user: User | None, action: str, request: Request, detail: dict | None = None) -> None:
    db.add(
        AuditLog(
            user_id=user.id if user else None,
            username=user.username if user else None,
            action=action,
            ip=request.client.host if request.client else None,
            detail=detail or {},
        )
    )


@router.get("/info", response_model=AuthInfo)
async def auth_info() -> AuthInfo:
    return AuthInfo(
        local_auth_enabled=settings.local_auth_enabled,
        oidc_enabled=settings.oidc_enabled and bool(settings.oidc_issuer and settings.oidc_client_id),
        oidc_display_name=settings.oidc_display_name,
    )


@router.post("/login", response_model=UserOut)
async def login(payload: LoginRequest, request: Request, response: Response, db: DB) -> User:
    if not settings.local_auth_enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "本地账号登录已关闭，请使用统一登录")

    user = (
        await db.execute(select(User).where(User.username == payload.username))
    ).scalar_one_or_none()

    # 无论用户是否存在都走一次校验路径，避免通过响应时间枚举账号
    if user is None or not verify_password(payload.password, user.password_hash):
        await _audit(db, None, "login_failed", request, {"username": payload.username})
        await db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "账号已被禁用")

    user.last_login_at = datetime.now(UTC)
    await _audit(db, user, "login", request, {"method": "local"})
    await db.commit()

    _set_session_cookie(response, user)
    return user


@router.post("/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(settings.session_cookie_name, path="/")
    return {"ok": True}


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> User:
    return user


@router.patch("/preferences", response_model=UserOut)
async def update_preferences(payload: MyPreferences, user: CurrentUser, db: DB) -> User:
    """用户自己改通知开关与接收邮箱。"""
    data = payload.model_dump(exclude_unset=True)
    if "notify_email" in data and data["notify_email"] is not None:
        user.notify_email = data["notify_email"]
    if "email" in data:
        email = (data["email"] or "").strip()
        if email and "@" not in email:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "邮箱格式不正确")
        user.email = email or None
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/change-password")
async def change_password(payload: ChangePasswordRequest, user: CurrentUser, db: DB) -> dict:
    if user.auth_source != AuthSource.local:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "统一登录账号请到身份提供方修改密码")
    if not verify_password(payload.old_password, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "原密码不正确")
    user.password_hash = hash_password(payload.new_password)
    await db.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# OIDC
# --------------------------------------------------------------------------- #
def _require_oidc() -> None:
    if not (settings.oidc_enabled and settings.oidc_issuer and settings.oidc_client_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "未启用 OIDC 登录")


@router.get("/oidc/login")
async def oidc_login(redirect_after: str = "/") -> RedirectResponse:
    _require_oidc()
    try:
        url = await oidc_client.build_authorize_url(redirect_after)
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"无法访问 IdP discovery 端点: {exc}") from exc
    return RedirectResponse(url, status_code=status.HTTP_302_FOUND)


@router.get("/oidc/callback")
async def oidc_callback(
    request: Request,
    db: DB,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> RedirectResponse:
    _require_oidc()
    base = settings.frontend_base_url.rstrip("/")

    if error:
        detail = error_description or error
        return RedirectResponse(f"{base}/login?{urlencode({'error': detail})}", status_code=302)
    if not code or not state:
        return RedirectResponse(f"{base}/login?error={quote('缺少 code 或 state')}", status_code=302)

    try:
        claims, redirect_after = await oidc_client.exchange_code(code, state)
    except (ValueError, httpx.HTTPError) as exc:
        log.warning("OIDC 回调失败: %s", exc)
        return RedirectResponse(f"{base}/login?{urlencode({'error': str(exc)})}", status_code=302)

    try:
        user = await _upsert_oidc_user(db, claims)
    except PermissionError as exc:
        return RedirectResponse(f"{base}/login?{urlencode({'error': str(exc)})}", status_code=302)

    user.last_login_at = datetime.now(UTC)
    await _audit(db, user, "login", request, {"method": "oidc"})
    await db.commit()

    target = redirect_after if redirect_after.startswith("/") else "/"
    response = RedirectResponse(f"{base}{target}", status_code=302)
    _set_session_cookie(response, user)
    return response


async def _upsert_oidc_user(db: DB, claims: dict) -> User:
    subject = claims.get("sub")
    if not subject:
        raise PermissionError("IdP 未返回 sub")

    issuer = claims.get("iss") or settings.oidc_issuer
    username = (
        claims.get(settings.oidc_username_claim)
        or claims.get(settings.oidc_email_claim)
        or f"oidc_{subject[:12]}"
    )
    email = claims.get(settings.oidc_email_claim)
    display_name = claims.get(settings.oidc_name_claim) or username

    is_admin = False
    if settings.oidc_admin_group:
        groups = claims.get(settings.oidc_groups_claim) or []
        if isinstance(groups, str):
            groups = [groups]
        is_admin = settings.oidc_admin_group in groups

    user = (
        await db.execute(
            select(User).where(User.oidc_issuer == issuer, User.oidc_subject == subject)
        )
    ).scalar_one_or_none()

    if user is None:
        # 同名本地账号视为同一人，做一次账号绑定而不是建重复账号
        user = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
        if user is not None:
            user.oidc_issuer, user.oidc_subject = issuer, subject
        else:
            if not settings.oidc_auto_create_user:
                raise PermissionError("该账号尚未在平台注册，请联系管理员")
            first_user = (await db.execute(select(func.count(User.id)))).scalar_one() == 0
            user = User(
                username=username,
                auth_source=AuthSource.oidc,
                oidc_issuer=issuer,
                oidc_subject=subject,
                # 显式写死，不依赖列默认值 —— 默认值要 flush 之后才会回填，
                # 而下面的启用状态检查发生在 flush 之前
                is_active=True,
                # 首个登录用户自动成为管理员，避免全新部署没人能进后台
                role=UserRole.admin if (is_admin or first_user) else UserRole.user,
            )
            db.add(user)

    user.email = email or user.email
    user.display_name = display_name or user.display_name
    await db.flush()

    if not user.is_active:
        raise PermissionError("账号已被禁用")

    if settings.oidc_admin_group:
        # 配置了管理员组就以 IdP 为准同步角色，移出该组即失去管理员权限。
        # 唯一的例外是最后一个启用的管理员 —— 降掉它就没人能进后台了。
        if is_admin:
            user.role = UserRole.admin
        elif user.role == UserRole.admin and not await would_orphan_admin(db, user):
            user.role = UserRole.user
        await db.flush()

    return user


@router.get("/oidc/logout")
async def oidc_logout(response: Response) -> dict:
    """返回 IdP 的登出地址（若 IdP 支持），前端拿到后自行跳转。"""
    response.delete_cookie(settings.session_cookie_name, path="/")
    url = None
    if settings.oidc_enabled and settings.oidc_issuer:
        url = await oidc_client.end_session_url(settings.frontend_base_url.rstrip("/") + "/login")
    return {"ok": True, "end_session_url": url}
