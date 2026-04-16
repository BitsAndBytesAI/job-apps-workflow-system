#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PACKAGE_DIR="$ROOT_DIR/macos/JobAppsNative"
APP_NAME="JobAppsNative"
CONFIGURATION="${1:-debug}"
BUILD_DIR="$ROOT_DIR/macos/build/$CONFIGURATION"
APP_DIR="$BUILD_DIR/${APP_NAME}.app"
CONTENTS_DIR="$APP_DIR/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
RESOURCES_DIR="$CONTENTS_DIR/Resources"
BACKEND_RESOURCES_DIR="$RESOURCES_DIR/backend"
PYTHON_RESOURCES_DIR="$RESOURCES_DIR/python"
PLAYWRIGHT_RESOURCES_DIR="$RESOURCES_DIR/playwright-browsers"

if [[ "$CONFIGURATION" != "debug" && "$CONFIGURATION" != "release" ]]; then
  echo "Usage: $0 [debug|release]" >&2
  exit 1
fi

swift build --package-path "$PACKAGE_DIR" -c "$CONFIGURATION" >/dev/null
BIN_DIR="$(swift build --package-path "$PACKAGE_DIR" -c "$CONFIGURATION" --show-bin-path)"
EXECUTABLE="$BIN_DIR/$APP_NAME"

rm -rf "$APP_DIR"
mkdir -p "$MACOS_DIR" "$RESOURCES_DIR"
cp "$EXECUTABLE" "$MACOS_DIR/$APP_NAME"
chmod +x "$MACOS_DIR/$APP_NAME"

VENV_PYTHON="$ROOT_DIR/.venv/bin/python"
if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "Missing bundled Python source at $VENV_PYTHON. Create the virtualenv first." >&2
  exit 1
fi

PYTHON_BASE_PREFIX="$("$VENV_PYTHON" - <<'PY'
import sys
print(sys.base_prefix)
PY
)"

PYTHON_VERSION="$("$VENV_PYTHON" - <<'PY'
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}")
PY
)"

PLAYWRIGHT_FIREFOX_PACKAGE_DIR="$("$VENV_PYTHON" - <<'PY'
from pathlib import Path
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    print(Path(p.firefox.executable_path).parents[4])
PY
)"

cat > "$CONTENTS_DIR/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>en</string>
  <key>CFBundleExecutable</key>
  <string>$APP_NAME</string>
  <key>CFBundleIdentifier</key>
  <string>ai.bitsandbytes.jobapps.native</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleName</key>
  <string>Job Apps Workflow System</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>0.1.0</string>
  <key>CFBundleVersion</key>
  <string>1</string>
  <key>JobAppsBundledBackendRelativePath</key>
  <string>backend</string>
  <key>JobAppsBundledPythonRelativePath</key>
  <string>python/bin/python</string>
  <key>JobAppsBundledPlaywrightBrowsersRelativePath</key>
  <string>playwright-browsers</string>
  <key>LSApplicationCategoryType</key>
  <string>public.app-category.business</string>
  <key>LSMinimumSystemVersion</key>
  <string>15.0</string>
  <key>NSAppTransportSecurity</key>
  <dict>
    <key>NSAllowsArbitraryLoadsInWebContent</key>
    <true/>
    <key>NSAllowsLocalNetworking</key>
    <true/>
  </dict>
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>NSPrincipalClass</key>
  <string>NSApplication</string>
</dict>
</plist>
PLIST

rm -rf "$BACKEND_RESOURCES_DIR" "$PYTHON_RESOURCES_DIR" "$PLAYWRIGHT_RESOURCES_DIR"
mkdir -p "$BACKEND_RESOURCES_DIR/src/job_apps_system" "$PLAYWRIGHT_RESOURCES_DIR"

rsync -a "$ROOT_DIR/src/job_apps_system/" "$BACKEND_RESOURCES_DIR/src/job_apps_system/"
rsync -aL "$PYTHON_BASE_PREFIX/" "$PYTHON_RESOURCES_DIR/"
mkdir -p "$PYTHON_RESOURCES_DIR/lib/python${PYTHON_VERSION}/site-packages"
rsync -a "$ROOT_DIR/.venv/lib/python${PYTHON_VERSION}/site-packages/" "$PYTHON_RESOURCES_DIR/lib/python${PYTHON_VERSION}/site-packages/"
ln -sfn "python${PYTHON_VERSION}" "$PYTHON_RESOURCES_DIR/bin/python"
rsync -a "$PLAYWRIGHT_FIREFOX_PACKAGE_DIR/" "$PLAYWRIGHT_RESOURCES_DIR/$(basename "$PLAYWRIGHT_FIREFOX_PACKAGE_DIR")/"

printf 'APPL????' > "$CONTENTS_DIR/PkgInfo"

echo "Built $APP_DIR"
