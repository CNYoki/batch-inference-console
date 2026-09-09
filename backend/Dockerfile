# ---------- 构建阶段：编译 asyncmy 等带 C 扩展的依赖 ----------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1
WORKDIR /build

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential default-libmysqlclient-dev pkg-config \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY app ./app

# 装进独立的 venv，运行阶段整个拷过去 —— 编译器和头文件都不进最终镜像
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir .

# ---------- 运行阶段 ----------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# curl 供 healthcheck 使用；libmysqlclient 的运行时库随 asyncmy 静态带入，无需额外安装
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

RUN useradd -m -u 1000 appuser && mkdir -p /data && chown -R appuser:appuser /app /data
USER appuser

ENV DATA_DIR=/data
EXPOSE 8000

# 只对 API 有意义；worker 用同一镜像但不监听端口，compose 里已为它关掉
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
