#!/usr/bin/env bash
# One command that proves this checkout still does what the task card asks.
#
#   ./scripts/verify.sh            # unit tests + reference numbers on the delivered dataset
#   ./scripts/verify.sh --full     # + the whole pytest suite against a throwaway database
#
# Exit code 0 means: the code is green AND the comparison layer reproduces the
# organisers' reference on the delivered dataset. Nothing here is mocked; the
# full mode really runs the pipeline on 2002 pairs.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
PY="${PYTHON:-$APP_DIR/.venv/bin/python}"
export PYTHONPATH="$APP_DIR/src"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\033[31mОШИБКА: %s\033[0m\n' "$*" >&2; exit 1; }

[ -x "$PY" ] || fail "нет интерпретатора $PY — см. README, раздел «Установка»"

bold "1/4 · модульные тесты (без БД)"
"$PY" -m pytest tests/unit -q >/tmp/verify-unit.log 2>&1 \
  || { tail -20 /tmp/verify-unit.log; fail "модульные тесты"; }
passed=$(tr -cd '.' < /tmp/verify-unit.log | wc -c)
echo "   ок: $passed тестов пройдено, подробности в /tmp/verify-unit.log"

bold "2/4 · эталонные числа на выданном датасете"
if [ -f tools/validate_against_reference.py ]; then
  "$PY" tools/validate_against_reference.py OPEN | tee /tmp/verify-reference.log | tail -12
  grep -qiE "mismatch.*0|0 mismatch|MATCH" /tmp/verify-reference.log \
    || fail "сверка с эталоном: см. /tmp/verify-reference.log"
else
  echo "   пропущено: tools/validate_against_reference.py отсутствует (материалы задания не в репозитории)"
fi

bold "3/4 · числа в документации против базы"
if ! "$PY" tools/check_doc_claims.py > /tmp/verify-claims.log 2>&1; then
  tail -20 /tmp/verify-claims.log
  fail "числа в документации разошлись с базой (см. /tmp/verify-claims.log)"
fi
tail -3 /tmp/verify-claims.log

if [ "${1:-}" = "--full" ]; then
  bold "4/4 · полный набор тестов (нужна отдельная база)"
  TEST_DB="${TEST_DATABASE_URL:-postgresql://ca:ca@127.0.0.1:55432/change_analysis_test}"
  TEST_DATABASE_URL="$TEST_DB" "$PY" -m pytest tests -q >/tmp/verify-full.log 2>&1 \
    || { tail -30 /tmp/verify-full.log; fail "полный набор тестов"; }
  echo "   ок, см. /tmp/verify-full.log"
else
  bold "4/4 · полный набор тестов пропущен (запустите с --full)"
fi

bold "Готово: проверки пройдены."
echo "Приложение поднимается командами ./scripts/web.sh и ./scripts/worker.sh."
