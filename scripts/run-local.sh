#!/usr/bin/env bash
set -euo pipefail

source .venv/bin/activate
uvicorn job_apps_system.main:app --reload
