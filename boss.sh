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
  record)  exec "$PY" backend/boss_record.py ;;
  inspect) exec "$PY" backend/boss_inspect.py ;;
  fetch) shift; exec "$PY" backend/boss_cli.py fetch "$@" ;;
  whoami) exec "$PY" backend/boss_cli.py whoami ;;
  web)
    # 网页还没做 —— 采集器都还没有,先有数据再谈界面。
    echo "BOSS 的网页还没做。现在可用:"
    echo "  ./boss.sh record   ← 从这里开始:你正常用浏览器,程序在后台记录"
    echo "  ./boss.sh inspect  看录到了什么接口和字段"
    echo "  ./boss.sh login    只登录不记录"
    echo "  ./boss.sh whoami   看登录态还在不在"
    exit 2
    ;;
  *) echo "用法: ./boss.sh [web|login|fetch]"; exit 1 ;;
esac
