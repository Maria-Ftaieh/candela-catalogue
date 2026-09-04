#!/bin/sh
# Prepares the instance on first start, then serves it.
# Every step is idempotent, so restarting the container is cheap.
set -e

DB=data/catalogue.db
DEMO="${DEMO:-0}"

if [ "$DEMO" = "1" ] && [ ! -d brands/demo ]; then
  echo "==> Generating the fictional demo dataset"
  python examples/demo_data.py
fi

# Refuse to build an empty database: without source data the result is a working
# but pointless instance, and the cause is easy to miss.
if [ ! -f "$DB" ] && [ -z "$(find brands -mindepth 1 -maxdepth 1 -type d ! -name '_*' 2>/dev/null)" ]; then
  echo "ERROR: brands/ contains no brand directory, so there is nothing to load." >&2
  echo "       Mount your data at /app/brands — see brands/README.md." >&2
  echo "       To try the software with fictional data instead:" >&2
  echo "         docker compose -f docker-compose.demo.yml up" >&2
  exit 1
fi

if [ ! -f "$DB" ]; then
  echo "==> Building the database (first start; a full catalogue takes a few minutes)"
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
  USER_NAME="${ADMIN_USER:-admin}"
  USER_PASS="${ADMIN_PASSWORD:-}"

  if [ "$DEMO" = "1" ]; then
    # Demo accounts skip the forced password change, otherwise the published
    # credentials would stop working after the first visitor signs in.
    [ -n "$USER_PASS" ] || USER_PASS="demo1234demo"
    python etl/users.py add "$USER_NAME" --admin --name "Demo User" \
        --password "$USER_PASS" --no-password-change >/dev/null
  else
    GENERATED=0
    if [ -z "$USER_PASS" ]; then
      USER_PASS=$(python -c "import sys; sys.path.insert(0, '.'); from web import auth; \
                             print(auth.generate_password())")
      GENERATED=1
    fi
    # No --no-password-change here: in a real installation the first administrator
    # must replace this password on first sign-in.
    python etl/users.py add "$USER_NAME" --admin --password "$USER_PASS" >/dev/null
    echo "======================================================================"
    echo "  First administrator created."
    echo "    username : $USER_NAME"
    echo "    password : $USER_PASS"
    [ "$GENERATED" = "1" ] && echo "  (generated; it is not stored anywhere else)"
    echo "  You will be asked to choose a new password on first sign-in."
    echo "======================================================================"
  fi
fi

echo "==> Starting on http://0.0.0.0:${PORT:-8000}"
exec uvicorn web.app:app --host 0.0.0.0 --port "${PORT:-8000}"
