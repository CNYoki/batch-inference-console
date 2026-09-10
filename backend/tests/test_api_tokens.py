"""个人 API Token：签发、Bearer 鉴权、吊销、过期、权限边界。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from app.db import SessionLocal
from app.main import app
from app.models import ApiToken


def _bearer_client(token: str) -> AsyncClient:
    """不带 Cookie 的新客户端，只靠 Authorization 头鉴权。"""
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


async def _create(client: AsyncClient, name: str = "cli", days: int | None = 90) -> dict:
    resp = await client.post("/api/auth/tokens", json={"name": name, "expires_in_days": days})
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_token_is_shown_once_and_authenticates(admin_client: AsyncClient):
    created = await _create(admin_client, "laptop")
    token = created["token"]
    assert token.startswith("bic_") and created["token_prefix"] == token[:8]
    assert created["expires_at"] is not None

    listed = (await admin_client.get("/api/auth/tokens")).json()
    row = next(t for t in listed if t["id"] == created["id"])
    assert "token" not in row and row["last_used_at"] is None

    async with _bearer_client(token) as c:
        me = await c.get("/api/auth/me")
        assert me.status_code == 200 and me.json()["username"] == "admin"
        assert (await c.get("/api/jobs")).status_code == 200

    listed = (await admin_client.get("/api/auth/tokens")).json()
    assert next(t for t in listed if t["id"] == created["id"])["last_used_at"] is not None


@pytest.mark.asyncio
async def test_token_never_stored_in_clear(admin_client: AsyncClient):
    created = await _create(admin_client)
    async with SessionLocal() as db:
        row = (await db.execute(select(ApiToken).where(ApiToken.id == created["id"]))).scalar_one()
    assert created["token"] not in (row.token_hash, row.token_prefix, row.name)
    assert len(row.token_hash) == 64


@pytest.mark.asyncio
async def test_token_cannot_manage_tokens(admin_client: AsyncClient):
    token = (await _create(admin_client))["token"]
    async with _bearer_client(token) as c:
        assert (await c.get("/api/auth/tokens")).status_code == 403
        assert (await c.post("/api/auth/tokens", json={"name": "x"})).status_code == 403


@pytest.mark.asyncio
async def test_revoked_token_is_rejected(admin_client: AsyncClient):
    created = await _create(admin_client)
    assert (await admin_client.delete(f"/api/auth/tokens/{created['id']}")).status_code == 204
    async with _bearer_client(created["token"]) as c:
        resp = await c.get("/api/auth/me")
    assert resp.status_code == 401 and "吊销" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_expired_token_is_rejected(admin_client: AsyncClient):
    created = await _create(admin_client, days=1)
    async with SessionLocal() as db:
        await db.execute(
            update(ApiToken)
            .where(ApiToken.id == created["id"])
            .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        await db.commit()
    async with _bearer_client(created["token"]) as c:
        resp = await c.get("/api/auth/me")
    assert resp.status_code == 401 and "过期" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_never_expiring_token(admin_client: AsyncClient):
    created = await _create(admin_client, days=None)
    assert created["expires_at"] is None
    async with _bearer_client(created["token"]) as c:
        assert (await c.get("/api/auth/me")).status_code == 200


@pytest.mark.asyncio
async def test_unknown_token_is_rejected(client: AsyncClient):
    async with _bearer_client("bic_this-token-does-not-exist") as c:
        assert (await c.get("/api/auth/me")).status_code == 401


@pytest.mark.asyncio
async def test_tokens_are_private_and_follow_user_status(admin_client: AsyncClient):
    user = (await admin_client.post("/api/admin/users", json={
        "username": "token-user", "password": "TokenUser123!",
    })).json()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as other:
        assert (await other.post(
            "/api/auth/login", json={"username": "token-user", "password": "TokenUser123!"}
        )).status_code == 200
        mine = await _create(other, "mine")
        # 别人的 token 看不到也删不掉
        admin_ids = {t["id"] for t in (await admin_client.get("/api/auth/tokens")).json()}
        assert mine["id"] not in admin_ids
        assert (await admin_client.delete(f"/api/auth/tokens/{mine['id']}")).status_code == 404

    async with _bearer_client(mine["token"]) as c:
        assert (await c.get("/api/auth/me")).json()["username"] == "token-user"
        # token 不提权：普通用户的 token 进不了管理接口
        assert (await c.get("/api/admin/models")).status_code == 403

        await admin_client.patch(f"/api/admin/users/{user['id']}", json={"is_active": False})
        assert (await c.get("/api/auth/me")).status_code == 403


@pytest.mark.asyncio
async def test_token_name_and_expiry_validation(admin_client: AsyncClient):
    assert (await admin_client.post("/api/auth/tokens", json={"name": "  "})).status_code == 422
    assert (await admin_client.post(
        "/api/auth/tokens", json={"name": "x", "expires_in_days": 0}
    )).status_code == 422


@pytest.mark.asyncio
async def test_tokens_require_login(client: AsyncClient):
    assert (await client.get("/api/auth/tokens")).status_code == 401
