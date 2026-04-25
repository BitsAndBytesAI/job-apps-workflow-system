#!/usr/bin/env bash

if [[ -z "${ROOT_DIR:-}" ]]; then
  ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
fi

JOB_APPS_LOCAL_GOOGLE_OAUTH_CLIENT_CONFIG_DEFAULT="$ROOT_DIR/config/google-oauth-client.local.json"
JOB_APPS_SECRET_STORAGE_TEST_APP_DATA_DIR_DEFAULT="/tmp/jobapps-secret-storage-test"
JOB_APPS_SECRET_STORE_SERVICE="ai.bitsandbytes.jobapps.secret.v1"
JOB_APPS_KNOWN_SECRET_NAMES=(
  "openai_api_key"
  "anthropic_api_key"
  "anymailfinder_api_key"
  "google_oauth_token_json"
)

job_apps_read_env_value() {
  local key="$1"
  local env_file="$ROOT_DIR/.env"
  if [[ ! -f "$env_file" ]]; then
    return 0
  fi
  grep -E "^${key}=" "$env_file" | tail -n 1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//"
}

job_apps_resolve_google_oauth_client_config_path() {
  local configured="${GOOGLE_OAUTH_CLIENT_CONFIG_PATH:-}"
  if [[ -z "$configured" ]]; then
    configured="$(job_apps_read_env_value GOOGLE_OAUTH_CLIENT_CONFIG_PATH)"
  fi
  if [[ -n "$configured" ]]; then
    printf '%s\n' "$configured"
    return 0
  fi
  if [[ -f "$JOB_APPS_LOCAL_GOOGLE_OAUTH_CLIENT_CONFIG_DEFAULT" ]]; then
    printf '%s\n' "$JOB_APPS_LOCAL_GOOGLE_OAUTH_CLIENT_CONFIG_DEFAULT"
    return 0
  fi
  printf '\n'
}

job_apps_secret_storage_test_app_data_dir() {
  printf '%s\n' "${APP_DATA_DIR:-$JOB_APPS_SECRET_STORAGE_TEST_APP_DATA_DIR_DEFAULT}"
}
