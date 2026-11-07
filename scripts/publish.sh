#!/usr/bin/env bash
# Публикует приложение в интернет и ПРОВЕРЯЕТ, что канал действительно пригоден.
#
#   ./scripts/publish.sh            # поднять туннель, проверить, напечатать адрес
#   ./scripts/publish.sh --check    # только проверить текущий канал (без запуска)
#   ./scripts/publish.sh --stop     # остановить
#
# Почему проверка обязательна: туннель может «подняться» и отдавать HTML, но не
# пропускать файлы больше нескольких килобайт — тогда интерфейс откроется пустым.
# Скрипт качает реальный ресурс интерфейса и говорит прямо, годится канал или нет.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
PORT="${APP_PORT:-8000}"
LOG_DIR="${LOG_DIR:-/tmp/spr-demo}"
LOG="$LOG_DIR/tunnel.log"
PIDFILE="$LOG_DIR/tunnel.pid"
mkdir -p "$LOG_DIR"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

# Реальный крупный ресурс интерфейса: 47 КБ. Если он не доезжает, браузер покажет
# пустую страницу — именно это и надо ловить.
ASSET="/_nicegui/4.0.0/static/socket.io.min.js"

check_channel() {
  local url="$1"
  bold "Проверяю канал $url"

  local html_code html_size
  html_code="$(curl -s -o /dev/null --max-time 25 -w '%{http_code}' "$url/login" || true)"
  html_size="$(curl -s --max-time 25 "$url/login" | wc -c || echo 0)"
  if [ "$html_code" = "200" ]; then
    ok "страница входа: HTTP 200, ${html_size} байт"
  else
    warn "страница входа: HTTP ${html_code:-нет ответа}"
    return 1
  fi

  local asset_size
  asset_size="$(curl -s --max-time 30 -o /dev/null -w '%{size_download}' "$url$ASSET" || echo 0)"
  if [ "${asset_size:-0}" -ge 40000 ]; then
    ok "ресурс интерфейса: ${asset_size} байт — браузер загрузится"
    return 0
  fi
  warn "ресурс интерфейса: пришло ${asset_size:-0} из 47295 байт"
  echo "     Канал не пропускает файлы такого размера: страница откроется пустой."
  return 1
}

stop_tunnel() {
  bold "Останавливаю туннель"
  if [ -f "$PIDFILE" ]; then
    pid="$(cat "$PIDFILE")"
    kill "$pid" 2>/dev/null && ok "остановлен (pid $pid)"
    rm -f "$PIDFILE"
  fi
  pkill -f "cloudflared tunnel" 2>/dev/null && ok "cloudflared остановлен" || true
  pkill -f "localhost.run" 2>/dev/null && ok "ssh-туннель остановлен" || true
  pkill -f "a.pinggy.io" 2>/dev/null && ok "pinggy остановлен" || true
}

case "${1:-}" in
  --stop) stop_tunnel; exit 0 ;;
  --check)
    url="${2:-}"
    [ -n "$url" ] || die "укажите адрес: ./scripts/publish.sh --check https://..."
    check_channel "$url" || exit 1
    exit 0 ;;
esac

# приложение должно отвечать локально
curl -s -o /dev/null --max-time 3 "http://127.0.0.1:$PORT/login" \
  || die "приложение не отвечает на порту $PORT — сначала ./scripts/demo.sh"

rm -f "$LOG"
URL=""

if [ -f "$HOME/.cloudflared/cert.pem" ]; then
  NAME="$(sed -n 's/^tunnel: *//p' "$HOME/.cloudflared/config.yaml" 2>/dev/null | head -1)"
  HOSTNAME_CF="$(sed -n 's/^ *- hostname: *//p' "$HOME/.cloudflared/config.yaml" 2>/dev/null | head -1)"
  bold "Найден именованный туннель Cloudflare${NAME:+ (id $NAME)}"
  cloudflared tunnel run ${NAME:---url http://127.0.0.1:$PORT} >"$LOG" 2>&1 &
  echo $! >"$PIDFILE"
  sleep 12
  [ -n "$HOSTNAME_CF" ] && URL="https://$HOSTNAME_CF"
else
  bold "Именованного туннеля нет — поднимаю временный"
  cloudflared tunnel --url "http://127.0.0.1:$PORT" >"$LOG" 2>&1 &
  echo $! >"$PIDFILE"
  for _ in $(seq 1 30); do
    URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" 2>/dev/null | head -1 || true)"
    [ -n "$URL" ] && break
    sleep 1
  done
fi

if [ -z "$URL" ]; then
  warn "адрес не получен; пробую запасной SSH-туннель"
  ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ServerAliveInterval=20 \
      -R 80:localhost:"$PORT" nokey@localhost.run >"$LOG" 2>&1 &
  echo $! >"$PIDFILE"
  for _ in $(seq 1 30); do
    URL="$(grep -oE 'https://[a-z0-9]+\.lhr\.life' "$LOG" 2>/dev/null | head -1 || true)"
    [ -n "$URL" ] && break
    sleep 1
  done
fi

[ -n "$URL" ] || die "не удалось получить публичный адрес, см. $LOG"

echo
if check_channel "$URL"; then
  cat <<EOF

$(bold "Сайт опубликован:")
  $(bold "$URL")
  вход: analyst / analyst   (второй пользователь: analyst2 / analyst2)

Остановить: ./scripts/publish.sh --stop
EOF
else
  cat <<EOF

$(bold "Туннель поднят, но канал непригоден для интерфейса:")
  $URL

Так выглядит фильтрация трафика на этом подключении. Варианты:
  1) подключиться к другой сети (мобильная точка доступа) и запустить скрипт снова —
     туннель и домен уже настроены, ничего менять не нужно;
  2) запустить приложение на машине с чистым интернетом (VPS) — тогда доступ
     из любой сети.
Локальная сеть при этом работает: http://$(hostname -I | awk '{print $1}'):$PORT
EOF
  exit 2
fi
