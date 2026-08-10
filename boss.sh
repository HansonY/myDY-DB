#!/usr/bin/env bash
# BOSS 直聘知识库 —— **和抖音完全独立**:另一个 db、另一个登录态、另一个端口。
#
#   ./boss.sh          起网页  → http://localhost:8001
#   ./boss.sh login    扫码登录(首次跑一次,登录态存在本地 profile 目录)
#   ./boss.sh fetch    抓我自己的数据(投递/收藏/沟通)
#
# 为什么不做成一个应用里切「空间」:store.connect() 现在是全局读一个 db 路径,
# 改成多库要把 db 句柄穿过每一层 —— 对正在用的系统是高风险重构,
# 换来的只是少开一个标签页。两个实例反而更清楚:两个知识库就是两个东西。
set -euo pipefail
cd "$(dirname "$0")"

PY=.venv/bin/python
[ -x "$PY" ] || { echo "先跑 ./setup.sh"; exit 1; }

# 独立 db。抖音那个库一个字节都不会被碰。
export BOSS_DB_PATH="${BOSS_DB_PATH:-data/boss.db}"

case "${1:-web}" in
  login)   exec "$PY" backend/boss_cli.py login ;;
  snippet)
    # 把控制台片段打出来,方便直接复制
    cat scripts/boss_snippet.js
    echo
    echo "# ↑ 复制以上全部,粘到 zhipin.com 页面的 F12 → Console" >&2
    exit 0 ;;
  llmsniff)
    # key 到底是哪家的?挨个试一遍,不用改 .env 一家家换
    exec "$PY" -c "import sys;sys.path.insert(0,'backend');import llm,json;print(json.dumps(llm.sniff(),ensure_ascii=False,indent=1))" ;;
  llmtest)
    # 配完模型先跑这个 —— 各家端点和模型名改得勤,别等提取时才发现不通
    exec "$PY" -c "import sys;sys.path.insert(0,'backend');import llm,json;print(json.dumps(llm.probe(),ensure_ascii=False,indent=1))" ;;
  probe)   exec "$PY" backend/boss_probe.py ;;
  record)  exec "$PY" backend/boss_record.py ;;
  inspect) exec "$PY" backend/boss_inspect.py ;;
  fetch) shift; exec "$PY" backend/boss_cli.py fetch "$@" ;;
  whoami) exec "$PY" backend/boss_cli.py whoami ;;
  web)
    echo "BOSS 本地服务 → http://localhost:8001   (库:$BOSS_DB_PATH)"
    echo "插件会把你浏览过的岗位送到这里。"
    exec "$PY" -m uvicorn boss_main:app --app-dir backend --port 8001 --reload \
         --reload-dir backend
    ;;
  *) echo "用法: ./boss.sh [web|login|fetch]"; exit 1 ;;
esac
