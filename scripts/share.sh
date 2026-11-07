#!/usr/bin/env bash
# Publishes the running application to the internet through a temporary SSH tunnel.
#
#   ./scripts/share.sh            # поднять туннель и напечатать публичный адрес
#   ./scripts/share.sh --stop     # закрыть туннель
#
# Адрес действует, пока живёт туннель: это бесплатный туннель без аккаунта,
# у него нет постоянного имени. Для постоянного адреса нужен именованный туннель
# Cloudflare (свой домен) или ngrok с токеном — это уже с аккаунтом.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${APP_PORT:-8000}"
LOG="${LOG_DIR:-/tmp/spr-demo}/tunnel.log"
PIDFILE="${LOG_DIR:-/tmp/spr-demo}/tunnel.pid"
mkdir -p "$(dirname "$LOG")"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

if [ "${1:-}" = "--stop" ]; then
  if [ -f "$PIDFILE" ]; then
    pid="$(cat "$PIDFILE")"
    if kill "$pid" 2>/dev/null; then ok "туннель остановлен (pid $pid)"; fi
    rm -f "$PIDFILE"
  else
    # туннель мог быть запущен вручную
    pkill -f "localhost.run" 2>/dev/null && ok "туннель остановлен" || echo "  туннель не запущен"
  fi
  exit 0
fi

# приложение должно уже отвечать
if ! curl -s -o /dev/null --max-time 3 "http://127.0.0.1:${PORT}/login"; then
  die "приложение не отвечает на порту ${PORT} — сначала ./scripts/demo.sh"
fi

bold "Открываю публичный туннель"
rm -f "$LOG"
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes \
    -R 80:localhost:"$PORT" nokey@localhost.run >"$LOG" 2>&1 &
echo $! >"$PIDFILE"

URL=""
for _ in $(seq 1 40); do
  URL="$(grep -oE 'https://[a-z0-9]+\.lhr\.life' "$LOG" 2>/dev/null | head -1 || true)"
  [ -n "$URL" ] && break
  sleep 1
done
[ -n "$URL" ] || die "не удалось получить адрес, см. $LOG"

ok "туннель поднят (pid $(cat "$PIDFILE"))"
cat <<EOF

$(bold "Публичный адрес:")
  $(bold "$URL")
  вход: analyst / analyst   (второй пользователь: analyst2 / analyst2)

Ссылки в отчёте, скачанном через этот адрес, ведут обратно на него же.
Адрес живёт, пока работает туннель; после перезапуска он будет другим.

Закрыть: ./scripts/share.sh --stop
EOF
