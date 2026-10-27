#!/bin/sh
cd "$(dirname "$0")"
val() { grep "^$1=" .env | cut -d= -f2; }
PORT=$(val MONITOR_PORT); HOST=$(val MONITOR_HOST)
echo "Dashboard: http://localhost:${PORT:-9000}/?token=$(val MONITOR_TOKEN)"
echo "DSN:       http://$(val MONITOR_KEY)@<this-server-host>:${PORT:-9000}/<project id>"
exec .venv/bin/uvicorn server:app --host "${HOST:-127.0.0.1}" --port "${PORT:-9000}" --env-file .env
