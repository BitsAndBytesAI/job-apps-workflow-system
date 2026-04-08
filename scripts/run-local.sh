#!/usr/bin/env bash
set -euo pipefail

source .venv/bin/activate
export PYTHONPATH=src
python -m job_apps_system.cli.launch_backend
