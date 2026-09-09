.PHONY: help install dev-api dev-worker dev-web build test lint migrate up down logs

help:
	@echo "install     安装前后端依赖"
	@echo "dev-api     启动后端 API (http://127.0.0.1:8000)"
	@echo "dev-worker  启动一个 worker 进程"
	@echo "dev-web     启动前端开发服务器 (http://127.0.0.1:5180)"
	@echo "build       构建前端到 frontend/dist"
	@echo "test        运行后端测试"
	@echo "lint        运行 ruff 检查"
	@echo "migrate     执行数据库迁移到最新版本"
	@echo "up / down            docker compose 启停（自带 mysql+redis）"
	@echo "up-external / down-external  用现成的 mysql+redis 部署"

install:
	cd backend && uv venv --python 3.12 .venv && uv pip install -e ".[dev]"
	cd frontend && npm install

dev-api:
	cd backend && .venv/bin/uvicorn app.main:app --reload --port 8000

dev-worker:
	cd backend && .venv/bin/python -m app.worker.main

dev-web:
	cd frontend && npm run dev

build:
	cd frontend && npm run build

test:
	cd backend && .venv/bin/pytest tests/ -q

lint:
	cd backend && .venv/bin/ruff check app tests

migrate:
	cd backend && .venv/bin/alembic upgrade head

# 固定项目名：compose 默认拿目录名当项目名，目录一改卷名就变，
# 已有的数据卷会"消失"。写死之后目录随便改，数据卷不受影响。
COMPOSE = docker compose -p batch-inference

up:
	$(COMPOSE) up -d --build

# 用现成的 MySQL/Redis 部署（不自带数据库）
up-external:
	$(COMPOSE) -f docker-compose.external.yml up -d --build

down:
	$(COMPOSE) down

down-external:
	$(COMPOSE) -f docker-compose.external.yml down

logs:
	$(COMPOSE) logs -f api worker
