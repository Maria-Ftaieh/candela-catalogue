#!/bin/sh
# Prepares the instance on first start, then serves it.
# Every step is idempotent, so restarting the container is cheap.
set -e

DB=data/catalogue.db

if [ "${DEMO:-0}" = "1" ] && [ ! -d brands/demo ]; then
  echo "==> Generating the fictional demo dataset"
  python examples/demo_data.py
fi

if [ ! -f "$DB" ]; then
  echo "==> Building the database (first start; this takes a moment)"
  python etl/build_db.py
  python etl/index_docs.py
  python etl/link_catalogs.py
else
  echo "==> Database present, skipping the build"
fi

# Ask the auth module directly. Grepping the CLI output is not safe: when there are
# no accounts it prints a hint that itself contains the word "admin".
if ! python -c "import sys; sys.path.insert(0, '.'); from web import auth; \
       sys.exit(0 if auth.admin_count() else 1)"; then
  USER_NAME="${ADMIN_USER:-demo}"
  USER_PASS="${ADMIN_PASSWORD:-demo1234demo}"
  echo "==> Creating the first administrator: $USER_NAME"
  # Demo accounts skip the forced password change, otherwise the published
  # credentials would stop working after the first visitor signs in.
  python etl/users.py add "$USER_NAME" --admin --name "Demo User" \
      --password "$USER_PASS" --no-password-change >/dev/null
  echo "    username: $USER_NAME"
  echo "    password: $USER_PASS"
fi

echo "==> Starting on http://0.0.0.0:${PORT:-8000}"
exec uvicorn web.app:app --host 0.0.0.0 --port "${PORT:-8000}"
