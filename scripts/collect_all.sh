#!/usr/bin/env bash
# 顺序采集全部分类。刻意不并发 —— 同时打抖音多个接口是触发风控的主因。
#
# 用法:bash scripts/collect_all.sh
# 任一类被 403 掐断也不要紧:游标已落库,重跑本脚本会从断点续采。
set -uo pipefail
cd "$(dirname "$0")/.."

PY=.venv/bin/python
GAP="${GAP:-90}"   # 类别之间的间隔秒数,给风控留冷却时间

run() {
  local name="$1" cmd="$2"
  echo
  echo "═══ $name ═══"
  # 不用 set -e:某一类失败也要继续跑下一类
  # --line-buffered:否则 grep 会缓冲,进度看不到实时输出
  $PY backend/cli.py "$cmd" 2>&1 | grep --line-buffered -vE "^INFO|^WARNING|^─+|^\s*$"
  echo "─── $name 结束(退出码 ${PIPESTATUS[0]})"
}

run "收藏(续采)" favorites
sleep "$GAP"
run "点赞" likes
sleep "$GAP"
run "我的作品" posts

echo
echo "═══ 全部结束 ═══"
$PY backend/cli.py stats
