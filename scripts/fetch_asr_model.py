"""预下载 ASR 模型。单独成脚本 + 后台跑 —— large-v3-turbo 1.6GB 要十几分钟。

两个坑都在这儿踩过:
  1. HF 官方 CDN 在这台机器上连不上(xet CAS `error sending request`),
     必须走 hf-mirror.com。镜像地址由 config.py 在最上游设好 ——
     huggingface_hub 在 import 时就把它读死了,之后再改环境变量没用。
  2. 镜像本身也会中途断(实测下到 549MB/1.6GB 时 SSL EOF)。
     所以要重试 —— HF 的缓存支持续传,断点在 .incomplete 文件里,
     重跑接着下而不是从头来。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import config  # noqa: F401  —— 只为在 HF 之前设好 HF_ENDPOINT

from faster_whisper import WhisperModel

name = sys.argv[1] if len(sys.argv) > 1 else "large-v3-turbo"
tries = int(sys.argv[2]) if len(sys.argv) > 2 else 8

import os
print(f"下载 {name} · 镜像 {os.environ.get('HF_ENDPOINT')}", flush=True)
t0 = time.time()
for i in range(1, tries + 1):
    try:
        WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=4)
        print(f"✓ {name} 就绪 · 第 {i} 次 · 共 {time.time() - t0:.0f}s", flush=True)
        raise SystemExit(0)
    except SystemExit:
        raise
    except Exception as e:
        print(f"  第 {i}/{tries} 次失败 {type(e).__name__}: {str(e)[:100]} "
              f"(已用 {time.time() - t0:.0f}s,续传重试)", flush=True)
        time.sleep(min(5 * i, 30))
print(f"✗ {tries} 次都没成", flush=True)
raise SystemExit(1)
