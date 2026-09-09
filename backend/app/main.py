"""FastAPI 应用入口。

开发：  uvicorn app.main:app --reload --port 8000
生产：  uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import admin_settings, auth, jobs, model_configs, tools, users
from .bootstrap import init_database
from .config import settings
from .db import engine
from .services.queue import queue

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.ensure_dirs()
    await init_database(settings.auto_create_tables)
    if not await queue.ping():
        log.warning("Redis 连接失败（%s），任务将无法入队执行", settings.redis_url)
    yield
    await queue.close()
    await engine.dispose()


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
    docs_url=f"{settings.api_prefix}/docs",
    openapi_url=f"{settings.api_prefix}/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,  # 会话走 httpOnly Cookie，必须允许携带凭证
    allow_methods=["*"],
    allow_headers=["*"],
)

for module in (auth, jobs, model_configs, users, admin_settings, tools):
    app.include_router(module.router, prefix=settings.api_prefix)


@app.get(f"{settings.api_prefix}/health")
async def health() -> dict:
    return {
        "status": "ok",
        "redis": await queue.ping(),
        "oidc_enabled": settings.oidc_enabled,
        "local_auth_enabled": settings.local_auth_enabled,
    }


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("未处理异常 %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": f"服务器内部错误: {type(exc).__name__}"})


# --------------------------------------------------------------------------- #
# 生产模式下由后端直接托管前端构建产物（frontend/dist）
# --------------------------------------------------------------------------- #
_frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _frontend_dist.is_dir():
    app.mount("/assets", StaticFiles(directory=_frontend_dist / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> FileResponse:
        """把非 API 路径交给前端路由处理。"""
        candidate = _frontend_dist / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_frontend_dist / "index.html")
