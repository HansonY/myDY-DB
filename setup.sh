#!/usr/bin/env bash
# Douyin-DB 一键安装。clone 下来之后跑这个,然后跑 ./go.sh 就完事。
#
# 做的事:找一个可用的 Python → 建 venv → 装依赖 → 装扫码登录用的浏览器 →
#         生成 .env → 建库。全部幂等,可反复跑。
set -euo pipefail
cd "$(dirname "$0")"

say() { printf "\033[1m%s\033[0m\n" "$*"; }

# ── Python 版本:只能 3.10–3.13 ────────────────────────────────
# 3.14 装不上:pydantic-core / PyO3 目前最高支持 3.13,会掉进 Rust 编译错误。
# 这个坑是干净克隆测出来的,所以这里必须显式挑版本,不能用裸 python3。
PY=""
for c in python3.13 python3.12 python3.11 python3.10; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  # 退回 python3,但先确认版本在范围内
  if command -v python3 >/dev/null 2>&1 && \
     python3 -c 'import sys; sys.exit(0 if (3,10)<=sys.version_info[:2]<=(3,13) else 1)'; then
    PY=python3
  else
    echo "✗ 需要 Python 3.10–3.13。3.14 装不上(pydantic-core/PyO3 还不支持)。"
    echo "  macOS:  brew install python@3.13"
    exit 1
  fi
fi
say "① Python  $($PY -V)"

# ── venv ────────────────────────────────────────────────────
if [ ! -x .venv/bin/python ]; then
  "$PY" -m venv .venv
  say "② venv    已创建 .venv"
else
  say "② venv    已存在,跳过"
fi
PIP=".venv/bin/pip"
$PIP install -q --upgrade pip

# ── 依赖 ────────────────────────────────────────────────────
# f2 钉死 httpx==0.27.2 / pydantic==2.9.*,下游全部让它 —— 别在这里加约束。
say "③ 依赖    安装中(f2 会带一串固定版本的包)…"
$PIP install -q -r backend/requirements.txt

# 扫码登录要真浏览器。约 300 MB,只装一次。
say "④ 浏览器  扫码登录用(chromium,约 300MB,只装一次)…"
$PIP install -q -r backend/requirements-login.txt
.venv/bin/python -m playwright install chromium >/dev/null 2>&1 || {
  echo "   ⚠️  chromium 装失败。扫码登录用不了,但可以改用:"
  echo "      .venv/bin/python backend/cli.py login   # 从本机浏览器读 cookie"
}

# ── .env ────────────────────────────────────────────────────
# 里面会存 cookie —— 等同账号控制权,已在 .gitignore 里,绝不要提交或外传。
if [ ! -f .env ]; then
  cp .env.example .env
  say "⑤ 配置    已生成 .env(cookie 会写在这里,已 gitignore)"
else
  say "⑤ 配置    .env 已存在,跳过"
fi

# ── 建库 ────────────────────────────────────────────────────
.venv/bin/python backend/cli.py init

echo
say "装好了。下一步:"
echo "    ./go.sh          # 扫码登录 + 自动采集(可反复跑)"
echo
echo "  然后看数据:"
echo "    ./web.sh         # → http://localhost:8000"
