#!/usr/bin/env bash
# 生产方式启动：一个 uvicorn 同时提供 API 与前端页面（同源），外加若干 worker。
#
#   scripts/run.sh start [worker数]   启动（默认 1 个 worker）
#   scripts/run.sh stop               停止（等在跑的任务收尾，默认最多 60s）
#   scripts/run.sh stop force         立刻强杀，不等任务收尾
#   scripts/run.sh restart [worker数] 重启
#   scripts/run.sh status             查看状态
#   scripts/run.sh logs [api|worker]  跟踪日志
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/backend/.venv/bin"
RUN_DIR="$ROOT/data/run"
LOG_DIR="$ROOT/data/logs"
HOST="${BIND_HOST:-0.0.0.0}"
PORT="${BIND_PORT:-8000}"
# 等待 worker 优雅退出的秒数。worker 收到 TERM 后会先把在跑的任务收尾，
# 大任务可能要很久 —— 超时后不强杀，而是把它登记为 draining 继续跟踪。
STOP_TIMEOUT="${STOP_TIMEOUT:-60}"

mkdir -p "$RUN_DIR" "$LOG_DIR"

_alive() { [[ -f "$1" ]] && kill -0 "$(cat "$1")" 2>/dev/null; }

start() {
  local workers="${1:-1}"

  if [[ ! -f "$ROOT/frontend/dist/index.html" ]]; then
    echo "==> 前端未构建，先执行 npm run build"
    (cd "$ROOT/frontend" && npm run build)
  fi

  echo "==> 执行数据库迁移"
  (cd "$ROOT/backend" && "$PY/alembic" upgrade head 2>&1 | grep -E "Running upgrade|已是最新" || true)

  if _alive "$RUN_DIR/api.pid"; then
    echo "==> API 已在运行 (pid $(cat "$RUN_DIR/api.pid"))"
  else
    cd "$ROOT/backend"
    # 用 nohup 而不是 setsid：setsid 会 fork，$! 拿到的是包装进程的 pid，
    # 记进 pid 文件后 stop/status 就再也对不上真正的服务进程了
    nohup "$PY/uvicorn" app.main:app --host "$HOST" --port "$PORT" \
      --proxy-headers --forwarded-allow-ips='*' \
      > "$LOG_DIR/api.log" 2>&1 < /dev/null &
    echo $! > "$RUN_DIR/api.pid"
    disown %% 2>/dev/null || true
    echo "==> API 已启动 (pid $!) http://${HOST}:${PORT}"
  fi

  for i in $(seq 1 "$workers"); do
    local pidfile="$RUN_DIR/worker-$i.pid"
    if _alive "$pidfile"; then
      echo "==> worker-$i 已在运行 (pid $(cat "$pidfile"))"
      continue
    fi
    cd "$ROOT/backend"
    nohup "$PY/python" -m app.worker.main \
      > "$LOG_DIR/worker-$i.log" 2>&1 < /dev/null &
    echo $! > "$pidfile"
    disown %% 2>/dev/null || true
    echo "==> worker-$i 已启动 (pid $!)"
  done

  sleep 4
  _verify_pids
  status
}

_verify_pids() {
  # 记错 pid 会让 stop/status 全部失效，启动后立刻核对一次
  shopt -s nullglob
  for pidfile in "$RUN_DIR"/*.pid; do
    [[ "$(basename "$pidfile")" == *.draining.pid ]] && continue
    local pid; pid="$(cat "$pidfile")"
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "!!  $(basename "$pidfile" .pid) 的 pid $pid 不存在，请检查 $LOG_DIR 下的日志"
      continue
    fi
    if ! ps -p "$pid" -o args= 2>/dev/null | grep -qE "uvicorn|app\.worker\.main"; then
      echo "!!  pid $pid 不是本项目的进程，pid 文件可能记错了"
    fi
  done
}

stop() {
  local force="${1:-}"
  shopt -s nullglob

  # 1) 先统一发 TERM
  local pids=() names=()
  for pidfile in "$RUN_DIR"/*.pid; do
    local pid; pid="$(cat "$pidfile")"
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      pids+=("$pid"); names+=("$(basename "$pidfile" .pid)")
      echo "==> 已发送退出信号给 $(basename "$pidfile" .pid) (pid $pid)"
    else
      rm -f "$pidfile"
    fi
  done
  [[ ${#pids[@]} -eq 0 ]] && { echo "==> 没有正在运行的进程"; return; }

  # 2) 等它们真正退出。worker 会先把在跑的任务收尾，这期间不能算它已经停了 ——
  #    否则下面的 start 会拉起新进程，而这个还在跑的老进程就没人跟踪了
  echo "==> 等待进程退出（最多 ${STOP_TIMEOUT}s，worker 会先把在跑的任务收尾）"
  local waited=0
  while (( waited < STOP_TIMEOUT )); do
    local alive=0
    for pid in "${pids[@]}"; do kill -0 "$pid" 2>/dev/null && alive=1; done
    [[ $alive -eq 0 ]] && break
    sleep 2; waited=$((waited + 2))
  done

  # 3) 收尾：退干净的删 pidfile；还在 drain 的改名保留，status 仍能看到、以后还能再停
  local i=0
  for pid in "${pids[@]}"; do
    local name="${names[$i]}"; i=$((i + 1))
    if kill -0 "$pid" 2>/dev/null; then
      if [[ "$force" == "force" ]]; then
        kill -9 "$pid" 2>/dev/null || true
        rm -f "$RUN_DIR/$name.pid" "$RUN_DIR/$name.draining.pid"
        echo "==> 强制杀死 $name (pid $pid) —— 缓冲区里未落盘的结果会在恢复任务时重跑"
      else
        mv -f "$RUN_DIR/$name.pid" "$RUN_DIR/${name}.draining.pid" 2>/dev/null || true
        echo "==> $name (pid $pid) 仍在收尾未完成的任务，已登记为 draining 继续跟踪"
      fi
    else
      rm -f "$RUN_DIR/$name.pid" "$RUN_DIR/$name.draining.pid"
      echo "==> $name (pid $pid) 已退出"
    fi
  done

  shopt -s nullglob
  local draining=("$RUN_DIR"/*.draining.pid)
  if [[ ${#draining[@]} -gt 0 && "$force" != "force" ]]; then
    echo
    echo "提示：还有 ${#draining[@]} 个进程在收尾。它们不会再领新任务，跑完就会自己退出。"
    echo "      再执行一次 stop 可继续等待；确实要立刻中断用：scripts/run.sh stop force"
  fi
}

status() {
  shopt -s nullglob
  local any=0
  for pidfile in "$RUN_DIR"/*.pid; do
    local name; name="$(basename "$pidfile" .pid)"
    if [[ "$name" == *.draining ]]; then
      if _alive "$pidfile"; then
        any=1
        echo "  ${name%.draining}  收尾中 (pid $(cat "$pidfile")) —— 不再领新任务，跑完自动退出"
      else
        rm -f "$pidfile"   # 已经退干净了，清掉残留登记
      fi
      continue
    fi
    any=1
    if _alive "$pidfile"; then
      echo "  $name  运行中 (pid $(cat "$pidfile"))"
    else
      echo "  $name  已退出"
    fi
  done
  [[ $any -eq 0 ]] && echo "  （没有正在运行的进程）"
  echo -n "  健康检查: "
  curl -s -m 5 "http://127.0.0.1:${PORT}/api/health" || echo "无响应"
  echo
}

case "${1:-start}" in
  start)   start "${2:-1}" ;;
  stop)    stop "${2:-}" ;;
  restart) stop "${3:-}"; sleep 2; start "${2:-1}" ;;
  status)  status ;;
  logs)    tail -f "$LOG_DIR/${2:-api}"*.log ;;
  *) echo "用法: $0 {start|stop|restart|status|logs} [参数]"; exit 1 ;;
esac
