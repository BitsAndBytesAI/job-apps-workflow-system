#!/usr/bin/env bash
set -euo pipefail

source .venv/bin/activate
alembic upgrade head
