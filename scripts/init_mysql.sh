#!/usr/bin/env bash
# 交互式创建数据库与非特权账号，然后把 DATABASE_URL 写回 .env。
#
# 用法：
#   scripts/init_mysql.sh --host db.example.com --port 3306 --admin-user root
# 管理员密码从 stdin 读取，不会出现在命令行历史或进程列表里。
set -euo pipefail

HOST=127.0.0.1
PORT=3306
ADMIN_USER=root
DB_NAME=batch_inference
APP_USER=batchinfer
APP_HOST='%'

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --admin-user) ADMIN_USER="$2"; shift 2 ;;
    --db) DB_NAME="$2"; shift 2 ;;
    --app-user) APP_USER="$2"; shift 2 ;;
    --app-host) APP_HOST="$2"; shift 2 ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done

read -rsp "请输入 ${ADMIN_USER}@${HOST} 的密码: " ADMIN_PASS; echo
APP_PASS=$(python3 -c "import secrets;print(secrets.token_urlsafe(24))")

echo "==> 正在创建数据库 ${DB_NAME} 与账号 ${APP_USER}@${APP_HOST}"
MYSQL_PWD="$ADMIN_PASS" mysql -h "$HOST" -P "$PORT" -u "$ADMIN_USER" <<SQL
CREATE DATABASE IF NOT EXISTS \`${DB_NAME}\`
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '${APP_USER}'@'${APP_HOST}' IDENTIFIED BY '${APP_PASS}';
ALTER USER '${APP_USER}'@'${APP_HOST}' IDENTIFIED BY '${APP_PASS}';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, DROP, REFERENCES
  ON \`${DB_NAME}\`.* TO '${APP_USER}'@'${APP_HOST}';
FLUSH PRIVILEGES;
SQL

URL="mysql+asyncmy://${APP_USER}:${APP_PASS}@${HOST}:${PORT}/${DB_NAME}"
echo "==> 验证新账号可连接"
MYSQL_PWD="$APP_PASS" mysql -h "$HOST" -P "$PORT" -u "$APP_USER" -D "$DB_NAME" -e "SELECT 1;" > /dev/null
echo "    连接正常"

ENV_FILE="$(dirname "$0")/../.env"
if [[ -f "$ENV_FILE" ]]; then
  # 注释掉旧的 DATABASE_URL，追加新的，保留原文件便于回滚
  cp "$ENV_FILE" "$ENV_FILE.bak"
  sed -i 's/^DATABASE_URL=/#DATABASE_URL=/' "$ENV_FILE"
  printf '\nDATABASE_URL=%s\n' "$URL" >> "$ENV_FILE"
  echo "==> 已写入 $ENV_FILE（原文件备份为 .env.bak）"
else
  echo "==> 请把下面这行加入 .env："
  echo "DATABASE_URL=${URL}"
fi

echo
echo "接下来执行数据库迁移："
echo "  cd backend && .venv/bin/alembic upgrade head"
