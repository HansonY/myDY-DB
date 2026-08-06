#!/usr/bin/env bash
# 工作台:看数据 + 点按钮采集。→ http://localhost:8000
set -euo pipefail
cd "$(dirname "$0")"
# --reload:改了后端自动重启。
# 这个坑吃过三次 —— 改完 schema 或加了新路由但服务还跑着旧代码,
# 表现是「页面没数据」或「404/422」,而根因完全看不出来。
# 本地自部署场景 reload 没有任何代价,不该靠人记得重启。
exec .venv/bin/python -m uvicorn main:app --app-dir backend \
     --port "${PORT:-8000}" --reload --reload-dir backend
