#!/usr/bin/env bash
# Scheduled entry point (launchd). Logs to data/logs/.
set -uo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate
mkdir -p data/logs
exec jobpilot run "$@" >> "data/logs/run-$(date +%Y%m%d).log" 2>&1
