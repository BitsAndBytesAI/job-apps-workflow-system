#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/scripts/lib/local-config.sh"

APP_DATA_DIR="$(job_apps_secret_storage_test_app_data_dir)"

echo "Resetting secret storage test state"
echo "App data dir: $APP_DATA_DIR"
rm -rf "$APP_DATA_DIR"

if command -v security >/dev/null 2>&1; then
  for secret_name in "${JOB_APPS_KNOWN_SECRET_NAMES[@]}"; do
    security delete-generic-password \
      -s "$JOB_APPS_SECRET_STORE_SERVICE" \
      -a "$secret_name" >/dev/null 2>&1 || true
  done
else
  echo "warning: macOS 'security' CLI not found; keychain items were not cleared" >&2
fi

echo "Done"
