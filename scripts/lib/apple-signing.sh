#!/usr/bin/env bash

if [[ -z "${ROOT_DIR:-}" ]]; then
  ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
fi

source "$ROOT_DIR/scripts/lib/local-config.sh"

JOB_APPS_SECRET_HELPER_PROFILE_DEFAULT_NAME="Job_Apps_Secret_Helper_Developer_ID.provisionprofile"
JOB_APPS_SECRET_HELPER_BUNDLE_ID="ai.bitsandbytes.jobapps.secret-helper"

job_apps_resolve_codesign_identity() {
  local configured="${JOB_APPS_CODESIGN_IDENTITY:-}"
  if [[ -z "$configured" ]]; then
    configured="$(job_apps_read_env_value JOB_APPS_CODESIGN_IDENTITY)"
  fi
  if [[ -n "$configured" ]]; then
    printf '%s\n' "$configured"
    return 0
  fi

  security find-identity -v -p codesigning 2>/dev/null \
    | sed -n 's/^[[:space:]]*[0-9][[:space:]]*) [0-9A-F]\{40\} "\(Developer ID Application:.*\)"/\1/p' \
    | head -n 1
}

job_apps_resolve_helper_profile_path() {
  local configured="${JOB_APPS_SECRET_HELPER_PROVISIONING_PROFILE:-}"
  if [[ -z "$configured" ]]; then
    configured="$(job_apps_read_env_value JOB_APPS_SECRET_HELPER_PROVISIONING_PROFILE)"
  fi
  if [[ -n "$configured" ]]; then
    printf '%s\n' "$configured"
    return 0
  fi

  local candidates=(
    "$ROOT_DIR/config/$JOB_APPS_SECRET_HELPER_PROFILE_DEFAULT_NAME"
    "$HOME/Downloads/$JOB_APPS_SECRET_HELPER_PROFILE_DEFAULT_NAME"
    "$HOME/Documents/$JOB_APPS_SECRET_HELPER_PROFILE_DEFAULT_NAME"
  )

  local candidate
  for candidate in "${candidates[@]}"; do
    if [[ -f "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  printf '\n'
}

job_apps_decode_provisioning_profile() {
  local profile_path="$1"
  local output_path="$2"
  security cms -D -i "$profile_path" > "$output_path"
}

job_apps_plist_read() {
  local plist_path="$1"
  local key_path="$2"
  /usr/libexec/PlistBuddy -c "Print :$key_path" "$plist_path" 2>/dev/null
}

job_apps_profile_team_id() {
  local plist_path="$1"
  local team_id
  team_id="$(job_apps_plist_read "$plist_path" "Entitlements:com.apple.developer.team-identifier")"
  if [[ -n "$team_id" ]]; then
    printf '%s\n' "$team_id"
    return 0
  fi
  job_apps_plist_read "$plist_path" "TeamIdentifier:0"
}

job_apps_profile_application_identifier() {
  local plist_path="$1"
  job_apps_plist_read "$plist_path" "Entitlements:com.apple.application-identifier"
}

job_apps_resolve_application_identifier() {
  local profile_app_id="$1"
  local team_id="$2"
  local bundle_id="$3"
  if [[ "$profile_app_id" == *"*" ]]; then
    printf '%s.%s\n' "$team_id" "$bundle_id"
    return 0
  fi
  printf '%s\n' "$profile_app_id"
}

job_apps_identity_team_id() {
  local identity="$1"
  sed -n 's/.*(\([A-Z0-9]\{10\}\))$/\1/p' <<<"$identity"
}
