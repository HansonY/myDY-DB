#!/usr/bin/env bash
# BOSS 直聘知识库 —— **和抖音完全独立**:另一个 db、另一个登录态、另一个端口。
#
#   ./boss.sh          起网页  → http://localhost:8001
#   ./boss.sh login    扫码登录(首次跑一次,登录态存在本地 profile 目录)
#   ./boss.sh fetch    抓我自己的数据(投递/收藏/沟通)
#
#   ./boss.sh index          给岗位片段建/补向量索引(零网络,本地 bge-m3)
#   ./boss.sh index-status    只看索引现状,不加载模型
#   ./boss.sh find  <关键词>  语义检索岗位(全在本地,不需要任何 key)
#   ./boss.sh ask   <问题>    基于岗位库回答,强制带出处(要 LLM key)
#
# 为什么还是两个实例、两个端口:实体、分析口径、界面都不一样,
# 混在一个应用里会让每个查询都先要问「这行是视频还是岗位」。
#
# 但**「片段 → 向量 → 检索 → 问答」那一层已经共用了**(backend/kb/,业务无关)。
# db 句柄只穿过 kb/ 那一层,业务层各自独立 —— 所以这里的 index/find/ask
# 和抖音那侧跑的是同一份实现,只是绑了另一个 Space(knowledge/boss_space.py)。
# (这段注释以前写的是「不做多库因为要把句柄穿过每一层」。那是重构前的状况,
#  现在句柄只穿一层了 —— 注释和代码打架的话,下一个人会照注释办事。)
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
  index)        exec "$PY" scripts/boss_index.py "${@:2}" ;;
  index-status) exec "$PY" scripts/boss_index.py --status ;;
  find)
    shift
    [ $# -gt 0 ] || { echo "用法: ./boss.sh find <关键词>"; exit 1; }
    exec "$PY" - "$@" <<'EOF'
import sys
sys.path.insert(0, "backend")
import kb
from knowledge.boss_space import BOSS_SPACE
r = kb.bind(BOSS_SPACE).search(" ".join(sys.argv[1:]), limit=10)
th = r["thresholds"]
for lbl, key, mark in (("相关", "good", "✓"), ("可能相关", "maybe", "?")):
    if not r[key]:
        continue
    line = th["good"] if key == "good" else th["maybe"]
    print(f"{lbl}({len(r[key])} 条,≥{line}):")
    for it in r[key]:
        flag = "" if it.get("jd_state") == "have" else "  ⚠️没抓到职位描述"
        closed = "  ⚠️已关闭" if it.get("job_state") == "closed" else ""
        print(f"  {mark} {it['score']:.3f}  {(it.get('title') or '?')[:26]:<28}"
              f"{(it.get('company') or '?')[:12]:<14}{it.get('salary') or '':<14}"
              f"{flag}{closed}")
        print(f"        {(it.get('text') or '')[:90]}")
if r["verdict"] == "nothing":
    print(f"库里没有。最接近的分数:{r['nearest_below']}(下限 {th['maybe']})")
elif r["verdict"] == "only_maybe":
    print(f"\n⚠️ 全部落在「可能相关」—— 这两条分档线是在抖音内容上扫出来的,"
          f"\n   岗位 JD 是长段落,同样相关度算出来偏低。分数看得见,自己判断。")
print(f"\n模型 {r['model']}")
EOF
    ;;
  ask)
    shift
    [ $# -gt 0 ] || { echo "用法: ./boss.sh ask <问题>"; exit 1; }
    exec "$PY" - "$@" <<'EOF'
import sys
sys.path.insert(0, "backend")
import kb
from knowledge.boss_space import BOSS_SPACE
r = kb.bind(BOSS_SPACE).ask(" ".join(sys.argv[1:]))
print(r["answer"])
if not r["answered"]:
    print(f"\n({r['reason']};最接近 {r.get('nearest_scores')})")
    sys.exit(0)
print()
for c in r["citations"]:
    print(f"  [{c['n']}] {c.get('company') or '?'} · {(c.get('title') or '')[:30]}"
          f" · {c.get('salary') or ''}  {c.get('url') or ''}")
if r["dropped_bogus_citations"]:
    print(f"\n(剔掉了模型编的编号 {r['dropped_bogus_citations']})")
if r["only_maybe"]:
    print("\n⚠️ 依据全是「可能相关」那一档 —— 别当确定答案。")
EOF
    ;;
  web)
    echo "BOSS 本地服务 → http://localhost:8001   (库:$BOSS_DB_PATH)"
    echo "插件会把你浏览过的岗位送到这里。"
    exec "$PY" -m uvicorn boss_main:app --app-dir backend --port 8001 --reload \
         --reload-dir backend
    ;;
  *) echo "用法: ./boss.sh [web|login|fetch|index|index-status|find|ask]"; exit 1 ;;
esac
