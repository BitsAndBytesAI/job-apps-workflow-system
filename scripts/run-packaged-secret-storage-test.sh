#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/scripts/lib/local-config.sh"

APP_BUNDLE="$ROOT_DIR/macos/build/debug/JobAppsNative.app"
APP_EXECUTABLE="$APP_BUNDLE/Contents/MacOS/JobAppsNative"
APP_DATA_DIR="$(job_apps_secret_storage_test_app_data_dir)"

if [[ ! -x "$APP_EXECUTABLE" ]]; then
  echo "Missing packaged debug app at $APP_EXECUTABLE" >&2
  echo "Build it first with ./scripts/build-macos-app.sh debug" >&2
  exit 1
fi

echo "Launching packaged debug app"
echo "App data dir: $APP_DATA_DIR"

APP_DATA_DIR="$APP_DATA_DIR" "$APP_EXECUTABLE"
