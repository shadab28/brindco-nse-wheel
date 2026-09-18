#!/usr/bin/env bash
# Start/stop the Brindco Postgres warehouse. The cluster lives inside the
# project (data/pgdata, gitignored) and listens on 127.0.0.1:5440.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PGDATA="$ROOT/data/pgdata"
LOG="$ROOT/data/logs/postgres-5440.log"
PGBIN="${PGBIN:-/opt/homebrew/Cellar/postgresql@18/18.3/bin}"
OPTS="-p 5440 -c listen_addresses=127.0.0.1"

mkdir -p "$(dirname "$LOG")"

case "${1:-status}" in
  start)  "$PGBIN/pg_ctl" -D "$PGDATA" -o "$OPTS" -l "$LOG" start ;;
  stop)   "$PGBIN/pg_ctl" -D "$PGDATA" -m fast stop ;;
  restart) "$PGBIN/pg_ctl" -D "$PGDATA" -o "$OPTS" -l "$LOG" -m fast restart ;;
  status) "$PGBIN/pg_ctl" -D "$PGDATA" status ;;
  *) echo "usage: $0 {start|stop|restart|status}" >&2; exit 2 ;;
esac
