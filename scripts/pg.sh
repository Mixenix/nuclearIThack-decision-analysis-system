#!/usr/bin/env bash
# Project-local PostgreSQL cluster.
#
# The system cluster on :5432 has no role for our user and we must not touch it
# (PLAN.md §23.1: no system service changes without an explicit decision), so the
# project runs its own cluster on port 55432 with a data directory inside app/.
#
#   scripts/pg.sh init     create the cluster (idempotent)
#   scripts/pg.sh start    start it
#   scripts/pg.sh stop     stop it
#   scripts/pg.sh status   report status
#   scripts/pg.sh psql     open a psql shell
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PGDATA="${PGDATA:-$APP_DIR/.pgdata}"
PGPORT="${PGPORT:-55432}"
PGUSER_NAME="${PGUSER_NAME:-ca}"
PGDB="${PGDB:-change_analysis}"
SOCKDIR="$PGDATA/sock"

cluster_running() {
  pg_ctl -D "$PGDATA" status >/dev/null 2>&1
}

case "${1:-status}" in
  init)
    if [ -d "$PGDATA/base" ]; then
      echo "cluster already initialised at $PGDATA"
    else
      rm -rf "$PGDATA"
      mkdir -p "$PGDATA"
      initdb -D "$PGDATA" -U "$PGUSER_NAME" --auth=trust --encoding=UTF8 --locale=C.UTF-8 >/dev/null
      echo "initialised cluster at $PGDATA"
    fi
    mkdir -p "$SOCKDIR"
    cluster_running || { pg_ctl -D "$PGDATA" -o "-p $PGPORT -k $SOCKDIR -c listen_addresses=127.0.0.1" -l "$PGDATA/server.log" -w start; }
    psql -h 127.0.0.1 -p "$PGPORT" -U "$PGUSER_NAME" -d postgres -tAc \
      "SELECT 1 FROM pg_database WHERE datname='$PGDB'" | grep -q 1 \
      || psql -h 127.0.0.1 -p "$PGPORT" -U "$PGUSER_NAME" -d postgres -c "CREATE DATABASE $PGDB"
    # This host's PostgreSQL cannot load llvmjit (libLLVM is absent); a failed JIT
    # turns an expensive analytic query into an error, so turn JIT off.
    psql -h 127.0.0.1 -p "$PGPORT" -U "$PGUSER_NAME" -d postgres -qc "ALTER SYSTEM SET jit = off" >/dev/null
    psql -h 127.0.0.1 -p "$PGPORT" -U "$PGUSER_NAME" -d postgres -qc "ALTER SYSTEM SET work_mem = '64MB'" >/dev/null
    psql -h 127.0.0.1 -p "$PGPORT" -U "$PGUSER_NAME" -d postgres -qc "ALTER SYSTEM SET random_page_cost = 1.1" >/dev/null
    psql -h 127.0.0.1 -p "$PGPORT" -U "$PGUSER_NAME" -d postgres -qc "ALTER SYSTEM SET effective_cache_size = '6GB'" >/dev/null
    # Bulk loads write ~1.4M rows per run; fsync per commit dominates them on this
    # disk. Durability of the last seconds is an acceptable trade for a local
    # analytical workload (documented in docs/BENCHMARKS.md).
    psql -h 127.0.0.1 -p "$PGPORT" -U "$PGUSER_NAME" -d postgres -qc "ALTER SYSTEM SET synchronous_commit = off" >/dev/null
    psql -h 127.0.0.1 -p "$PGPORT" -U "$PGUSER_NAME" -d postgres -qc "ALTER SYSTEM SET maintenance_work_mem = '256MB'" >/dev/null
    psql -h 127.0.0.1 -p "$PGPORT" -U "$PGUSER_NAME" -d postgres -qc "SELECT pg_reload_conf()" >/dev/null
    echo "database $PGDB ready on 127.0.0.1:$PGPORT"
    ;;
  start)
    mkdir -p "$SOCKDIR"
    if cluster_running; then echo "already running"; else
      pg_ctl -D "$PGDATA" -o "-p $PGPORT -k $SOCKDIR -c listen_addresses=127.0.0.1" -l "$PGDATA/server.log" -w start
    fi
    ;;
  stop)
    cluster_running && pg_ctl -D "$PGDATA" -m fast -w stop || echo "not running"
    ;;
  status)
    cluster_running && echo "running on port $PGPORT (data: $PGDATA)" || echo "stopped (data: $PGDATA)"
    ;;
  psql)
    psql -h 127.0.0.1 -p "$PGPORT" -U "$PGUSER_NAME" -d "$PGDB"
    ;;
  *)
    echo "usage: $0 {init|start|stop|status|psql}" >&2
    exit 2
    ;;
esac
