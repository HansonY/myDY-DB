#!/usr/bin/env bash
# 工作台:看数据 + 点按钮采集。→ http://localhost:8000
set -euo pipefail
cd "$(dirname "$0")"
exec .venv/bin/python -m uvicorn main:app --app-dir backend --port "${PORT:-8000}"
