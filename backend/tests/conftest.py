"""测试用的独立环境：SQLite + 临时数据目录，导入 app 之前必须先设置好。"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="biu-tests-"))
os.environ.update(
    DATABASE_URL=f"sqlite+aiosqlite:///{_TMP / 'test.db'}",
    DATA_DIR=str(_TMP / "data"),
    SECRET_KEY="test-secret-key-for-unit-tests-only",
    REDIS_URL="redis://127.0.0.1:6379/15",
    LOCAL_AUTH_ENABLED="true",
    OIDC_ENABLED="false",
    BOOTSTRAP_ADMIN_USERNAME="admin",
    BOOTSTRAP_ADMIN_PASSWORD="TestAdmin123!",
    DEBUG="false",
)

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.bootstrap import init_database
from app.db import engine
from app.main import app


@pytest.fixture(scope="session")
def tmp_root() -> Path:
    return _TMP


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _database():
    await init_database()
    yield
    await engine.dispose()


@pytest_asyncio.fixture
async def client() -> AsyncClient:
    # 直接打到 ASGI app，不经过网络，也就不需要 lifespan/Redis
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=False
    ) as c:
        yield c


@pytest_asyncio.fixture
async def admin_client(client: AsyncClient) -> AsyncClient:
    resp = await client.post("/api/auth/login", json={"username": "admin", "password": "TestAdmin123!"})
    assert resp.status_code == 200, resp.text
    return client
