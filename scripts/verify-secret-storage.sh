#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/scripts/lib/local-config.sh"

APP_DATA_DIR="$(job_apps_secret_storage_test_app_data_dir)"
DATABASE_PATH="$APP_DATA_DIR/app.db"
secret_rows=""
sqlite_error=""
sqlite_status=0
keychain_hits=0

echo "Secret storage verification"
echo "App data dir: $APP_DATA_DIR"
echo "Database path: $DATABASE_PATH"
echo

if [[ -f "$DATABASE_PATH" ]]; then
  set +e
  secret_rows="$(sqlite3 "$DATABASE_PATH" 'select key from app_settings where key like "secret:%" order by key;' 2>&1)"
  sqlite_status=$?
  set -e
  if [[ $sqlite_status -ne 0 ]]; then
    sqlite_error="$secret_rows"
    secret_rows=""
  fi
else
  echo "SQLite database not found yet."
fi

if [[ -n "$sqlite_error" ]]; then
  echo "SQLite check failed: $sqlite_error"
  exit 1
fi

if [[ -n "$secret_rows" ]]; then
  echo "FAIL: secret rows are still present in SQLite:"
  printf '%s\n' "$secret_rows"
  exit 1
fi

echo "PASS: no secret rows found in SQLite."
echo

if ! command -v security >/dev/null 2>&1; then
  echo "Keychain check skipped: macOS 'security' CLI not found."
  exit 0
fi

echo "Keychain items:"
for secret_name in "${JOB_APPS_KNOWN_SECRET_NAMES[@]}"; do
  if security find-generic-password \
    -s "$JOB_APPS_SECRET_STORE_SERVICE" \
    -a "$secret_name" >/dev/null 2>&1; then
    echo "  present: $secret_name"
    keychain_hits=$((keychain_hits + 1))
  else
    echo "  missing: $secret_name"
  fi
done

echo
if [[ $keychain_hits -eq 0 ]]; then
  echo "No known keychain items are present yet. This is fine before Setup completes."
else
  echo "PASS: found $keychain_hits keychain item(s)."
fi
