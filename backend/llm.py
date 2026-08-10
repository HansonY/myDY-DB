"""统一的 LLM 出口 —— 换供应商只改配置,不改代码。

之前把通义千问的端点和模型名写死在提取代码里,结果想换 MiniMax 就得改代码。
这几家(千问 / MiniMax / DeepSeek / 月之暗面 / 智谱 / 本地 Ollama)**都是
OpenAI 兼容的 chat/completions**,差别只有三个:base_url、model、api_key。
所以做成一张表 + 三个可覆盖的配置项。

配置(写 .env):
    LLM_PROVIDER=qwen | minimax | deepseek | moonshot | zhipu | ollama | custom
    LLM_API_KEY=…            不填就回退去找该家的专用键(见 _KEY_FALLBACK)
    LLM_BASE_URL=…           想用表里没有的服务就填这个,provider=custom
    LLM_MODEL=…              覆盖默认模型

⚠️ 表里的端点和模型名**我没有全部实测过**。各家改版很勤,所以:
  1. 三项都可以在 .env 里覆盖
  2. 提供 `probe()` 自检 —— 配完先跑一次,别等到提取时才发现不通
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

from config import ROOT, settings

# provider → (base_url, 默认模型)
# 默认模型都挑「快而便宜、够做提取」的那档 —— 提取是结构化任务,
# 不需要推理型号,那些又慢又贵。
_PROVIDERS: dict[str, tuple[str, str]] = {
    "qwen":     ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    # MiniMax 的路径**不是** /chat/completions,而是 /text/chatcompletion_v2。
    # 打错路径它的网关会先过认证、直接回 401,看起来像 key 无效 —— 很误导。
    "minimax":  ("https://api.minimaxi.com/v1/text/chatcompletion_v2", "MiniMax-Text-01"),
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "moonshot": ("https://api.moonshot.cn/v1", "moonshot-v1-32k"),
    "zhipu":    ("https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
    "ollama":   ("http://localhost:11434/v1", "qwen2.5:7b"),
}

# 没配 LLM_API_KEY 时,按 provider 去找它自己的键 ——
# 抖音那侧一直用 DASHSCOPE_API_KEY,不能因为加了这一层就让它失效。
_KEY_FALLBACK = {
    "qwen": "DASHSCOPE_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
    "zhipu": "ZHIPU_API_KEY",
}


class NoKey(RuntimeError):
    """没配 key。调用方应把原始数据留着待处理,不要丢。"""


def _env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if v:
        return v
    f = ROOT / ".env"
    if f.exists():
        for ln in f.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln.startswith(f"{name}="):
                return ln.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def config() -> dict[str, str]:
    """当前生效的 LLM 配置。**不含 key 的值**,只说配没配。"""
    prov = (_env("LLM_PROVIDER") or "qwen").lower()
    base, model = _PROVIDERS.get(prov, _PROVIDERS["qwen"])
    base = _env("LLM_BASE_URL") or base
    model = _env("LLM_MODEL") or model
    key = _env("LLM_API_KEY")
    if not key:
        alt = _KEY_FALLBACK.get(prov)
        if alt:
            key = _env(alt) or (settings.dashscope_api_key.strip()
                                if alt == "DASHSCOPE_API_KEY" else "")
    return {"provider": prov, "base_url": base, "model": model, "_key": key}


def available() -> bool:
    c = config()
    # ollama 跑在本机,不需要 key
    return bool(c["_key"]) or c["provider"] == "ollama"


def _url(base: str) -> str:
    """拼出最终的 chat 地址。

    base_url 里已经带了完整路径(比如 MiniMax 的 /text/chatcompletion_v2)就直接用,
    只给到 /v1 的才补 /chat/completions。写死拼接会把 MiniMax 打成 404/401。
    """
    b = base.rstrip("/")
    return b if re.search(r"/(chat/completions|chatcompletion\w*)$", b) \
        else b + "/chat/completions"


def _check_body(prov: str, data: Any) -> None:
    """有些家**返回 HTTP 200,错误藏在 body 里**。

    实测 MiniMax:`HTTP 200 {"base_resp":{"status_code":2049,"status_msg":"invalid api key"}}`。
    只看 HTTP 状态码会把它当成成功,然后在取 choices 时莫名崩掉 ——
    报错信息完全看不出根因是认证失败。所以必须查业务码。
    """
    if not isinstance(data, dict):
        return
    br = data.get("base_resp")
    if isinstance(br, dict) and br.get("status_code") not in (0, None):
        raise RuntimeError(
            f"{prov} 业务错误 {br.get('status_code')}: {br.get('status_msg')}")
    if isinstance(data.get("error"), dict):
        raise RuntimeError(f"{prov} 错误: {str(data['error'])[:180]}")


def chat_json(system: str, user: str, timeout: float = 180.0,
              temperature: float = 0.1) -> Any:
    """要一个 JSON 回复。返回已解析的对象。

    `response_format=json_object` 不是所有家都支持,所以**不依赖它** ——
    带上是锦上添花,解析时照样兜围栏和前后废话。
    """
    c = config()
    if not c["_key"] and c["provider"] != "ollama":
        raise NoKey(f"没配 {c['provider']} 的 API key")

    headers = {"Content-Type": "application/json"}
    if c["_key"]:
        headers["Authorization"] = f"Bearer {c['_key']}"

    r = httpx.post(
        _url(c["base_url"]),
        headers=headers,
        json={
            "model": c["model"],
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        },
        timeout=timeout,
        # 国内接口绕开系统代理 —— 抖音那边实测走代理必 SSL EOF
        trust_env=False,
    )
    if r.status_code >= 400:
        # 把服务端的原话带出来 —— 「模型名不对」「余额不足」这类只有它说得清
        raise RuntimeError(f"{c['provider']} HTTP {r.status_code}: {r.text[:220]}")
    payload = r.json()
    _check_body(c["provider"], payload)          # 200 也可能是错的,见上
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(
            f"{c['provider']} 返回体不认识:{str(payload)[:200]}") from e
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
    try:
        return json.loads(content)
    except ValueError:
        # 有些模型会在 JSON 前后多说两句 —— 抠出最外层大括号再试一次
        m = re.search(r"\{.*\}", content, re.S)
        if m:
            return json.loads(m.group())
        raise RuntimeError(f"{c['model']} 没返回合法 JSON:{content[:200]}")


def probe() -> dict[str, Any]:
    """自检:当前配置到底能不能用。

    各家端点和模型名改得很勤,而我表里的默认值**没有全部实测过** ——
    与其等到提取时才炸,不如配完先跑这个。
    """
    c = config()
    out = {"provider": c["provider"], "base_url": c["base_url"],
           "model": c["model"], "key_set": bool(c["_key"])}
    if not c["_key"] and c["provider"] != "ollama":
        out["ok"] = False
        out["error"] = f"没配 key。填 LLM_API_KEY 或 {_KEY_FALLBACK.get(c['provider'], '?')}"
        return out
    try:
        got = chat_json('只输出 JSON:{"ok":true}', "回一个 {\"ok\":true}", timeout=45)
        out["ok"] = bool(isinstance(got, dict) and got.get("ok"))
        out["reply"] = got
    except Exception as e:      # noqa: BLE001
        out["ok"] = False
        out["error"] = f"{type(e).__name__}: {str(e)[:220]}"
        msg = out["error"]
        if "2049" in msg or "invalid api key" in msg.lower() or "401" in msg:
            out["hint"] = (
                "认证没过。MiniMax 有两种 key:API Keys 页面那个只能用于"
                "**按量付费**;如果你的账号是 Token Plan / Credits 套餐,"
                "要用 Plan Details 里的 **subscription Key**。"
                "另外国内(api.minimax.chat)和国际(api.minimaxi.com)"
                "是两套独立账号,key 不通用。")
        else:
            out["hint"] = ("模型名或端点可能不对 —— 在 .env 里用 LLM_MODEL / "
                           "LLM_BASE_URL 覆盖成该家文档上的值")
    return out


def sniff() -> dict[str, Any]:
    """key 在哪家能用?**挨个试一遍**,不用改 .env 一家家换。

    为什么需要这个:各家 key 的前缀越来越像(`sk-…` 现在满地都是),
    而 401 只会说「invalid api key」,不会告诉你「你把 A 家的 key 填给 B 家了」。
    与其让人凭猜改配置,不如一次问清楚。

    只报状态码和服务端原话,**不打印 key**。
    """
    key = config()["_key"]
    if not key:
        return {"error": "没配 key —— 先填 LLM_API_KEY"}

    # 顺便试 MiniMax 的两个域:国内和国际是分开的,key 不通用
    cands = [
        ("minimax(国际)", "https://api.minimaxi.com/v1/text/chatcompletion_v2", "MiniMax-Text-01"),
        ("minimax(国内)", "https://api.minimax.chat/v1/text/chatcompletion_v2", "MiniMax-Text-01"),
        ("qwen", *_PROVIDERS["qwen"]),
        ("deepseek", *_PROVIDERS["deepseek"]),
        ("moonshot", *_PROVIDERS["moonshot"]),
        ("zhipu", *_PROVIDERS["zhipu"]),
    ]
    rows = []
    for name, base, model in cands:
        row: dict[str, Any] = {"试": name, "model": model}
        try:
            r = httpx.post(
                _url(base),
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                json={"model": model, "temperature": 0,
                      "messages": [{"role": "user", "content": "hi"}]},
                timeout=25, trust_env=False,
            )
            row["http"] = r.status_code
            if r.status_code < 400:
                # 200 也可能带业务错误(MiniMax 就是),必须查 body
                try:
                    _check_body(name, r.json())
                    row["结果"] = "✓ 这家能用"
                except Exception as e:      # noqa: BLE001
                    row["结果"] = "✗ " + str(e)[:70]
            else:
                # 400 常常是「key 对但模型名不对」—— 那也是有用的信号
                body = r.text[:150]
                row["结果"] = ("key 对,模型名不对" if r.status_code == 400
                               else "✗ 认证失败" if r.status_code in (401, 403)
                               else "✗")
                row["原话"] = body
        except Exception as e:      # noqa: BLE001
            row["结果"] = f"✗ 连不上 {type(e).__name__}"
        rows.append(row)
    return {"results": rows,
            "note": "看到「✓ 这家能用」就把 LLM_PROVIDER 改成那家;"
                    "看到「key 对,模型名不对」就用 LLM_MODEL 覆盖模型名"}
