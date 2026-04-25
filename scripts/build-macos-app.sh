#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMMON_GIT_DIR="$(git -C "$ROOT_DIR" rev-parse --git-common-dir 2>/dev/null || true)"
COMMON_ROOT=""
if [[ -n "$COMMON_GIT_DIR" ]]; then
  COMMON_ROOT="$(cd "$(dirname "$COMMON_GIT_DIR")" && pwd)"
fi
source "$ROOT_DIR/scripts/lib/local-config.sh"
source "$ROOT_DIR/scripts/lib/apple-signing.sh"
PACKAGE_DIR="$ROOT_DIR/macos/JobAppsNative"
APP_NAME="JobAppsNative"
HELPER_NAME="JobAppsSecretHelper"
SCHEDULER_AGENT_NAME="JobAppsSchedulerAgent"
APP_BUNDLE_ID="ai.bitsandbytes.jobapps.native"
HELPER_BUNDLE_ID="ai.bitsandbytes.jobapps.secret-helper"
CONFIGURATION="${1:-debug}"
BUILD_DIR="$ROOT_DIR/macos/build/$CONFIGURATION"
APP_DIR="$BUILD_DIR/${APP_NAME}.app"
CONTENTS_DIR="$APP_DIR/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
RESOURCES_DIR="$CONTENTS_DIR/Resources"
HELPERS_DIR="$CONTENTS_DIR/Helpers"
BACKEND_RESOURCES_DIR="$RESOURCES_DIR/backend"
PYTHON_RESOURCES_DIR="$RESOURCES_DIR/python"
PLAYWRIGHT_RESOURCES_DIR="$RESOURCES_DIR/playwright-browsers"
GOOGLE_OAUTH_CLIENT_RESOURCE="$RESOURCES_DIR/google-oauth-client.json"
HELPER_APP_DIR="$HELPERS_DIR/${HELPER_NAME}.app"
HELPER_CONTENTS_DIR="$HELPER_APP_DIR/Contents"
HELPER_MACOS_DIR="$HELPER_CONTENTS_DIR/MacOS"
SCHEDULER_AGENT_RESOURCE="$RESOURCES_DIR/$SCHEDULER_AGENT_NAME"
ENTITLEMENTS_DIR="$BUILD_DIR/entitlements"
HELPER_ENTITLEMENTS_PATH="$ENTITLEMENTS_DIR/${HELPER_NAME}.entitlements"
SIGNING_INFO_PATH="$ENTITLEMENTS_DIR/signing-info.plist"

if [[ "$CONFIGURATION" != "debug" && "$CONFIGURATION" != "release" ]]; then
  echo "Usage: $0 [debug|release]" >&2
  exit 1
fi

swift build --package-path "$PACKAGE_DIR" -c "$CONFIGURATION" >/dev/null
BIN_DIR="$(swift build --package-path "$PACKAGE_DIR" -c "$CONFIGURATION" --show-bin-path)"
EXECUTABLE="$BIN_DIR/$APP_NAME"
HELPER_EXECUTABLE="$BIN_DIR/$HELPER_NAME"
SCHEDULER_AGENT_EXECUTABLE="$BIN_DIR/$SCHEDULER_AGENT_NAME"

rm -rf "$APP_DIR" "$ENTITLEMENTS_DIR"
mkdir -p "$MACOS_DIR" "$RESOURCES_DIR" "$HELPERS_DIR" "$HELPER_CONTENTS_DIR" "$HELPER_MACOS_DIR"
cp "$EXECUTABLE" "$MACOS_DIR/$APP_NAME"
chmod +x "$MACOS_DIR/$APP_NAME"
cp "$HELPER_EXECUTABLE" "$HELPER_MACOS_DIR/$HELPER_NAME"
chmod +x "$HELPER_MACOS_DIR/$HELPER_NAME"
cp "$SCHEDULER_AGENT_EXECUTABLE" "$SCHEDULER_AGENT_RESOURCE"
chmod +x "$SCHEDULER_AGENT_RESOURCE"

VENV_PYTHON=""
for candidate in "$ROOT_DIR/.venv/bin/python" "$COMMON_ROOT/.venv/bin/python"; do
  if [[ -x "$candidate" ]]; then
    VENV_PYTHON="$candidate"
    break
  fi
done
if [[ -z "$VENV_PYTHON" ]]; then
  echo "Missing bundled Python source at $ROOT_DIR/.venv/bin/python. Create the virtualenv first." >&2
  exit 1
fi
VENV_ROOT="$(cd "$(dirname "$VENV_PYTHON")/.." && pwd)"

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
GOOGLE_OAUTH_CLIENT_CONFIG_PATH="${GOOGLE_OAUTH_CLIENT_CONFIG_PATH:-$(job_apps_resolve_google_oauth_client_config_path)}"
CODESIGN_IDENTITY="$(job_apps_resolve_codesign_identity)"
HELPER_PROVISIONING_PROFILE_PATH="$(job_apps_resolve_helper_profile_path)"
HELPER_PROFILE_PLIST=""
HELPER_TEAM_ID=""
HELPER_APPLICATION_IDENTIFIER=""

if [[ -n "$HELPER_PROVISIONING_PROFILE_PATH" && -f "$HELPER_PROVISIONING_PROFILE_PATH" ]]; then
  mkdir -p "$ENTITLEMENTS_DIR"
  HELPER_PROFILE_PLIST="$SIGNING_INFO_PATH"
  job_apps_decode_provisioning_profile "$HELPER_PROVISIONING_PROFILE_PATH" "$HELPER_PROFILE_PLIST"
  HELPER_TEAM_ID="$(job_apps_profile_team_id "$HELPER_PROFILE_PLIST")"
  HELPER_APPLICATION_IDENTIFIER="$(job_apps_resolve_application_identifier \
    "$(job_apps_profile_application_identifier "$HELPER_PROFILE_PLIST")" \
    "$HELPER_TEAM_ID" \
    "$HELPER_BUNDLE_ID")"
fi

if [[ -z "$HELPER_TEAM_ID" && -n "$CODESIGN_IDENTITY" ]]; then
  HELPER_TEAM_ID="$(job_apps_identity_team_id "$CODESIGN_IDENTITY")"
fi

if [[ -z "$HELPER_APPLICATION_IDENTIFIER" && -n "$HELPER_TEAM_ID" ]]; then
  HELPER_APPLICATION_IDENTIFIER="${HELPER_TEAM_ID}.${HELPER_BUNDLE_ID}"
fi

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
  <string>$APP_BUNDLE_ID</string>
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
  <key>JobAppsBundledGoogleOAuthClientRelativePath</key>
  <string>google-oauth-client.json</string>
  <key>JobAppsBundledSecretHelperRelativePath</key>
  <string>../Helpers/JobAppsSecretHelper.app/Contents/MacOS/JobAppsSecretHelper</string>
  <key>JobAppsBundledSchedulerAgentRelativePath</key>
  <string>JobAppsSchedulerAgent</string>
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

cat > "$HELPER_CONTENTS_DIR/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>en</string>
  <key>CFBundleExecutable</key>
  <string>$HELPER_NAME</string>
  <key>CFBundleIdentifier</key>
  <string>$HELPER_BUNDLE_ID</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleName</key>
  <string>Job Apps Secret Helper</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>0.1.0</string>
  <key>CFBundleVersion</key>
  <string>1</string>
  <key>JobAppsExpectedTeamIdentifier</key>
  <string>${HELPER_TEAM_ID}</string>
  <key>LSMinimumSystemVersion</key>
  <string>15.0</string>
</dict>
</plist>
PLIST

rm -rf "$BACKEND_RESOURCES_DIR" "$PYTHON_RESOURCES_DIR" "$PLAYWRIGHT_RESOURCES_DIR" "$GOOGLE_OAUTH_CLIENT_RESOURCE"
mkdir -p "$BACKEND_RESOURCES_DIR/src/job_apps_system" "$PLAYWRIGHT_RESOURCES_DIR"

rsync -a "$ROOT_DIR/src/job_apps_system/" "$BACKEND_RESOURCES_DIR/src/job_apps_system/"
rsync -aL "$PYTHON_BASE_PREFIX/" "$PYTHON_RESOURCES_DIR/"
mkdir -p "$PYTHON_RESOURCES_DIR/lib/python${PYTHON_VERSION}/site-packages"
rsync -a "$VENV_ROOT/lib/python${PYTHON_VERSION}/site-packages/" "$PYTHON_RESOURCES_DIR/lib/python${PYTHON_VERSION}/site-packages/"
ln -sfn "python${PYTHON_VERSION}" "$PYTHON_RESOURCES_DIR/bin/python"
rsync -a "$PLAYWRIGHT_FIREFOX_PACKAGE_DIR/" "$PLAYWRIGHT_RESOURCES_DIR/$(basename "$PLAYWRIGHT_FIREFOX_PACKAGE_DIR")/"
if [[ -n "${GOOGLE_OAUTH_CLIENT_CONFIG_PATH:-}" && -f "$GOOGLE_OAUTH_CLIENT_CONFIG_PATH" ]]; then
  cp "$GOOGLE_OAUTH_CLIENT_CONFIG_PATH" "$GOOGLE_OAUTH_CLIENT_RESOURCE"
else
  echo "Warning: Google OAuth client config was not bundled. Set GOOGLE_OAUTH_CLIENT_CONFIG_PATH, add it to .env, or place it at config/google-oauth-client.local.json before building." >&2
fi

printf 'APPL????' > "$CONTENTS_DIR/PkgInfo"
printf 'APPL????' > "$HELPER_CONTENTS_DIR/PkgInfo"

if [[ -n "$CODESIGN_IDENTITY" ]]; then
  mkdir -p "$ENTITLEMENTS_DIR"

  if [[ -n "$HELPER_PROVISIONING_PROFILE_PATH" && -f "$HELPER_PROVISIONING_PROFILE_PATH" ]]; then
    cp "$HELPER_PROVISIONING_PROFILE_PATH" "$HELPER_CONTENTS_DIR/embedded.provisionprofile"
    /usr/libexec/PlistBuddy -c "Add :com.apple.application-identifier string $HELPER_APPLICATION_IDENTIFIER" "$HELPER_ENTITLEMENTS_PATH"
    /usr/libexec/PlistBuddy -c "Add :com.apple.developer.team-identifier string $HELPER_TEAM_ID" "$HELPER_ENTITLEMENTS_PATH"
    plutil -convert xml1 "$HELPER_ENTITLEMENTS_PATH"
  elif [[ "$CONFIGURATION" == "release" ]]; then
    echo "Missing helper provisioning profile. Set JOB_APPS_SECRET_HELPER_PROVISIONING_PROFILE or place $JOB_APPS_SECRET_HELPER_PROFILE_DEFAULT_NAME in config/ or ~/Downloads." >&2
    exit 1
  else
    echo "Warning: helper provisioning profile not found. Signed debug build will not have helper entitlements." >&2
  fi

  CODESIGN_TIMESTAMP_FLAG="--timestamp=none"
  if [[ "$CONFIGURATION" == "release" ]]; then
    CODESIGN_TIMESTAMP_FLAG="--timestamp"
  fi

  codesign --force --sign "$CODESIGN_IDENTITY" $CODESIGN_TIMESTAMP_FLAG "$SCHEDULER_AGENT_RESOURCE"
  HELPER_CODESIGN_ARGS=(
    --force
    --sign "$CODESIGN_IDENTITY"
    --options runtime
    "$CODESIGN_TIMESTAMP_FLAG"
  )
  if [[ -f "$HELPER_ENTITLEMENTS_PATH" ]]; then
    HELPER_CODESIGN_ARGS+=(--entitlements "$HELPER_ENTITLEMENTS_PATH")
  fi
  codesign "${HELPER_CODESIGN_ARGS[@]}" "$HELPER_APP_DIR"
  codesign --force --sign "$CODESIGN_IDENTITY" --options runtime $CODESIGN_TIMESTAMP_FLAG "$APP_DIR"
  codesign --verify --strict --verbose=2 "$HELPER_APP_DIR"
  codesign --verify --strict --verbose=2 "$APP_DIR"
else
  if [[ "$CONFIGURATION" == "release" ]]; then
    echo "Missing Developer ID code-signing identity. Set JOB_APPS_CODESIGN_IDENTITY or install one in the login keychain." >&2
    exit 1
  fi
  echo "Warning: no Developer ID signing identity found. Debug app will remain unsigned and helper keychain access will fail." >&2
fi

echo "Built $APP_DIR"
