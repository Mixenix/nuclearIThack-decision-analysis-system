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


local_ip() {
  local ip=""
  if command -v ip >/dev/null 2>&1; then
    # берём адрес обычного интерфейса (wlp/enp/eth), а не VPN-туннеля
    ip="$(ip -4 -o addr show 2>/dev/null \
          | awk '$2 ~ /^(wl|en|et)/ {print $4}' | cut -d/ -f1 | head -1)"
  fi
  if [ -z "$ip" ] && command -v hostname >/dev/null 2>&1; then
    ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  fi
  if [ -z "$ip" ]; then
    ip="$(awk '$1=="00000000" {printf "%d.%d.%d.%d", strtonum("0x" substr($2,7,2)), strtonum("0x" substr($2,5,2)), strtonum("0x" substr($2,3,2)), strtonum("0x" substr($2,1,2))}' /proc/net/route 2>/dev/null | head -1)"
  fi
  echo "${ip:-127.0.0.1}"
}

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

# Порядок выбора канала:
#   1) именованный туннель по токену — единственный, что работает за CGNAT и через VPN;
#   2) именованный туннель по сертификату (локальный конфиг);
#   3) временный туннель Cloudflare;
#   4) запасной SSH-туннель.
CF_BIN="${CF_BIN:-cloudflared}"
[ -x /tmp/cf-new ] && CF_BIN=/tmp/cf-new
TOKEN_FILE="$HOME/.cloudflared/graeae.token"
HOST_FILE="$HOME/.cloudflared/graeae-host.txt"
URL=""

wait_for_registration() {
  for _ in $(seq 1 40); do
    grep -q "Registered tunnel connection" "$LOG" 2>/dev/null && return 0
    sleep 1
  done
  return 1
}

if [ -f "$TOKEN_FILE" ]; then
  bold "Именованный туннель Cloudflare (по токену)"
  "$CF_BIN" tunnel --protocol http2 --edge-ip-version 4 --no-autoupdate \
      run --token "$(cat "$TOKEN_FILE")" >"$LOG" 2>&1 &
  echo $! >"$PIDFILE"
  wait_for_registration && ok "туннель зарегистрирован" || warn "регистрация не подтвердилась"
  HOST="$(head -1 "$HOST_FILE" 2>/dev/null || true)"
  [ -n "$HOST" ] && URL="https://$HOST"
elif [ -f "$HOME/.cloudflared/cert.pem" ]; then
  bold "Именованный туннель Cloudflare (по сертификату)"
  NAME="$(sed -n 's/^tunnel: *//p' "$HOME/.cloudflared/config.yaml" 2>/dev/null | head -1)"
  HOST="$(sed -n 's/^ *- hostname: *//p' "$HOME/.cloudflared/config.yaml" 2>/dev/null | head -1)"
  "$CF_BIN" tunnel run ${NAME:+"$NAME"} >"$LOG" 2>&1 &
  echo $! >"$PIDFILE"
  wait_for_registration || true
  [ -n "$HOST" ] && URL="https://$HOST"
fi

if [ -z "$URL" ]; then
  bold "Поднимаю временный туннель Cloudflare"
  "$CF_BIN" tunnel --url "http://127.0.0.1:$PORT" --protocol http2 --edge-ip-version 4 \
      >"$LOG" 2>&1 &
  echo $! >"$PIDFILE"
  for _ in $(seq 1 30); do
    URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" 2>/dev/null | head -1 || true)"
    [ -n "$URL" ] && break
    sleep 1
  done
fi

if [ -z "$URL" ]; then
  warn "адрес не получен; пробую запасной SSH-туннель"
  pkill -f "cf-new tunnel" 2>/dev/null || true
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

Причина измерена (не догадка):
  • cloudflared ходит на edge по порту 7844 — соединение регистрируется и умирает
    через ~6 с, дальше 502/530 на каждый запрос;
  • обычный HTTPS на 443 с этой же машины работает: 20 МБ скачиваются за 5.6 с,
    отправка 1 МБ — за 0.6 с, то есть полоса и MTU в порядке;
  • SSH-туннели (serveo, pinggy, localhost.run) отдают HTML (12 КБ), но обрывают
    передачу файлов интерфейса на ~12 КБ — интерфейс остаётся пустым;
  • bore.pub (порт 7835) вообще не отвечает.
Вывод: фильтруются нестандартные порты и длительные туннельные сессии, а не наши
данные. Приложение здесь ни при чём.

Варианты:
  1) подключиться к другой сети (мобильная точка доступа) и запустить скрипт снова —
     туннель и домен уже настроены, ничего менять не нужно;
  2) запустить приложение на машине с чистым интернетом (VPS) — тогда доступ
     из любой сети.
Локальная сеть при этом работает: http://$(local_ip):$PORT
EOF
  exit 2
fi
