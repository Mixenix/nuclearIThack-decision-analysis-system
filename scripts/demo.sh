#!/usr/bin/env bash
# Starts everything needed for the demonstration and prints the address to open.
#
#   ./scripts/demo.sh            # PostgreSQL + web + worker, logs in /tmp
#   ./scripts/demo.sh --stop     # stop what this script started
#
# Idempotent: an already running database or web app is reused instead of being
# duplicated, so the same command is safe to run twice during a rehearsal.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
PY="${PYTHON:-$APP_DIR/.venv/bin/python}"
export PYTHONPATH="$APP_DIR/src"
LOG_DIR="${LOG_DIR:-/tmp/spr-demo}"
mkdir -p "$LOG_DIR"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

port_open() {
  "$PY" - "$1" <<'PY'
import socket, sys
sock = socket.socket()
sock.settimeout(0.5)
sys.exit(0 if sock.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
}

stop_all() {
  bold "Останавливаю демонстрационные процессы"
  for name in web worker; do
    pidfile="$LOG_DIR/$name.pid"
    if [ -f "$pidfile" ]; then
      pid="$(cat "$pidfile")"
      if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
        ok "остановлен $name (pid $pid)"
      fi
      rm -f "$pidfile"
    fi
  done
}

if [ "${1:-}" = "--stop" ]; then
  stop_all
  exit 0
fi

[ -x "$PY" ] || die "нет интерпретатора $PY — см. README §3 «Установка»"

bold "1 · PostgreSQL"
if ./scripts/pg.sh status >/dev/null 2>&1; then
  ok "кластер уже работает (порт 55432)"
else
  ./scripts/pg.sh start >"$LOG_DIR/pg.log" 2>&1 || die "не удалось поднять кластер, см. $LOG_DIR/pg.log"
  ok "кластер поднят"
fi

bold "2 · схема, пользователи, датасет"
if "$PY" -m change_analysis.cli bootstrap >"$LOG_DIR/bootstrap.log" 2>&1; then
  ok "схема и демо-пользователи готовы (analyst / analyst, analyst2 / analyst2)"
else
  die "bootstrap не прошёл, см. $LOG_DIR/bootstrap.log"
fi

bold "3 · worker (отдельный процесс)"
if [ -f "$LOG_DIR/worker.pid" ] && kill -0 "$(cat "$LOG_DIR/worker.pid")" 2>/dev/null; then
  ok "worker уже запущен (pid $(cat "$LOG_DIR/worker.pid"))"
else
  nohup "$PY" -m change_analysis.worker.main >"$LOG_DIR/worker.log" 2>&1 &
  echo $! >"$LOG_DIR/worker.pid"
  ok "worker запущен (pid $(cat "$LOG_DIR/worker.pid"), лог $LOG_DIR/worker.log)"
fi

bold "4 · веб-приложение"
if port_open 8000; then
  if grep -q "NiceGUI ready to go" "$LOG_DIR/web.log" 2>/dev/null; then
    ok "приложение уже слушает порт 8000"
  else
    warn "порт 8000 занят другим процессом — возможно, это уже приложение"
  fi
else
  nohup "$PY" main.py >"$LOG_DIR/web.log" 2>&1 &
  echo $! >"$LOG_DIR/web.pid"
  for _ in $(seq 1 40); do
    port_open 8000 && break
    sleep 0.5
  done
  port_open 8000 || die "приложение не поднялось, см. $LOG_DIR/web.log"
  ok "приложение запущено (pid $(cat "$LOG_DIR/web.pid"), лог $LOG_DIR/web.log)"
fi

BASE="$("$PY" - <<'PY'
import sys
sys.path.insert(0, "src")
from change_analysis.config import load_settings
print(load_settings().public_base_url)
PY
)"

cat <<EOF

$(bold "Готово. Открывайте:")
  $(bold "$BASE")
  вход: analyst / analyst   (второй пользователь: analyst2 / analyst2)

Демонстрационные прогоны уже в базе: откройте «Прогоны» и выберите
«Демонстрация: открытая часть (2002)» — 502 различия, 385 смен решения.

Остановить всё: ./scripts/demo.sh --stop
EOF
