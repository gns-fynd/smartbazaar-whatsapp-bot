#!/bin/sh
# Make the database directory (a mounted disk on Render) writable, then run as the app user.
set -eu

data_dir=$(dirname "${DATABASE_PATH:-/var/data/bot.sqlite3}")
if [ "$(id -u)" = "0" ]; then
    mkdir -p "$data_dir"
    chown app:app "$data_dir"
    chmod 700 "$data_dir"
    exec setpriv --reuid=app --regid=app --init-groups \
        uvicorn smartbazaar.app:create_app --factory --host 0.0.0.0 --port "${PORT:-8080}" --no-access-log
fi
exec uvicorn smartbazaar.app:create_app --factory --host 0.0.0.0 --port "${PORT:-8080}" --no-access-log
