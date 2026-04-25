#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/scripts/lib/local-config.sh"
source .venv/bin/activate
export PYTHONPATH=src
GOOGLE_OAUTH_CLIENT_CONFIG_PATH="${GOOGLE_OAUTH_CLIENT_CONFIG_PATH:-$(job_apps_resolve_google_oauth_client_config_path)}"
if [[ -n "$GOOGLE_OAUTH_CLIENT_CONFIG_PATH" ]]; then
  export GOOGLE_OAUTH_CLIENT_CONFIG_PATH
fi
python -m job_apps_system.cli.launch_backend
