#!/usr/bin/env bash
# 采集。首次会开浏览器让你扫码,之后直接采。
# 可反复跑 —— 每步都跳过已完成的,采集本身断点续跑,被 403 打断也接着走。
set -euo pipefail
cd "$(dirname "$0")"
exec .venv/bin/python backend/cli.py go "$@"
