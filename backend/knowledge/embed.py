"""文本 ↔ 向量。可插拔,不碰数据库。

**为什么做成接口而不是写死一个模型。** 向量是派生数据,全库重建只要 53 秒 ——
所以换模型不是单向门。出了更好的双语模型、或者哪天英文内容多了想换更强的,
改一行配置重跑就行。写死反而是给自己上枷锁。

默认 bge-m3。四个候选在真实数据上实测(详见 docs/SEARCH.md),
按「用中文问和用英文问同一话题是否命中同一条视频」这个双语硬指标:

    bge-small-zh 0/8 · multilingual-e5-small 1/8 · Qwen3-Embedding-0.6B 1/8
    bge-m3 4/8   ← 唯一及格

代价是 2.2 GB 模型、嵌入 53 秒,而且中文阈值余量从 +0.061 降到 +0.025 ——
换英文能力要付中文的钱,不是免费升级。
"""

from __future__ import annotations

import os
from typing import Protocol

import numpy as np

from config import settings


class Embedder(Protocol):
    """所有后端都要满足的形状。

    doc 和 query 分开两个方法,不是多余 —— 有些模型要求查询侧加指令前缀
    (bge-small-zh 要「为这个句子生成表示以用于检索相关文章:」,e5 要
    「query: 」),文档侧不加。混用会让相似度整体偏移,而这个项目的阈值
    是按分数绝对值定的,一偏就全废。
    bge-m3 两侧都不需要前缀,所以它这两个方法是一样的。
    """

    name: str
    dim: int

    def encode_docs(self, texts: list[str]) -> np.ndarray: ...
    def encode_query(self, text: str) -> np.ndarray: ...


# 模型 → (文档前缀, 查询前缀)。不在表里的按「都不加」处理。
_PREFIX = {
    "BAAI/bge-small-zh-v1.5": ("", "为这个句子生成表示以用于检索相关文章:"),
    "BAAI/bge-large-zh-v1.5": ("", "为这个句子生成表示以用于检索相关文章:"),
    "intfloat/multilingual-e5-small": ("passage: ", "query: "),
    "intfloat/multilingual-e5-base": ("passage: ", "query: "),
    "intfloat/multilingual-e5-large": ("passage: ", "query: "),
    # bge-m3 明确不需要前缀
}


class LocalEmbedder:
    """本机跑。收藏不出这台机器 —— 和当初否掉云 ASR 是同一个理由。"""

    def __init__(self, model: str):
        # 延迟导入:sentence-transformers 在 requirements-search.txt 里,
        # 是可选依赖。只做采集和字面搜索的人不该被迫装 2 GB 的 torch。
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise RuntimeError(
                "缺少语义检索依赖。装一下:\n"
                "  .venv/bin/pip install -r backend/requirements-search.txt\n"
                "  .venv/bin/pip install 'click==8.1.7'   # 见该文件里的冲突说明"
            ) from e

        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        self.name = model
        self._doc_prefix, self._query_prefix = _PREFIX.get(model, ("", ""))
        self._m = SentenceTransformer(model)
        self.dim = int(self._m.get_sentence_embedding_dimension())

    def encode_docs(self, texts: list[str]) -> np.ndarray:
        # normalize 之后点积就是 cosine,查询时不用再算模长
        return self._m.encode(
            [self._doc_prefix + t for t in texts],
            batch_size=64, normalize_embeddings=True, show_progress_bar=False,
        ).astype(np.float32)

    def encode_query(self, text: str) -> np.ndarray:
        return self._m.encode(
            [self._query_prefix + text], normalize_embeddings=True,
        ).astype(np.float32)[0]


class DashScopeEmbedder:
    """云端。零本地依赖、秒级完成,代价是私人收藏要发给第三方。

    留这条路是因为有人可能不想在自己机器上放 2 GB 模型。
    默认**不走**这条 —— 项目的一贯选择是本地。
    """

    def __init__(self, model: str = "text-embedding-v3"):
        if not settings.dashscope_api_key.strip():
            raise RuntimeError("DASHSCOPE_API_KEY 未配置(.env),无法用云端嵌入")
        self.name = f"dashscope:{model}"
        self.dim = 1024
        self._model = model

    def _call(self, texts: list[str]) -> np.ndarray:
        import httpx
        out: list[list[float]] = []
        # 官方单次上限 25 条
        for i in range(0, len(texts), 25):
            r = httpx.post(
                "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings",
                headers={"Authorization": f"Bearer {settings.dashscope_api_key.strip()}"},
                json={"model": self._model, "input": texts[i:i + 25],
                      "dimensions": self.dim, "encoding_format": "float"},
                timeout=60,
            )
            r.raise_for_status()
            out += [d["embedding"] for d in r.json()["data"]]
        v = np.asarray(out, dtype=np.float32)
        # 云端不保证归一化,自己来 —— 否则和本地后端的分数不同尺度,
        # 阈值会失效
        n = np.linalg.norm(v, axis=1, keepdims=True)
        return v / np.maximum(n, 1e-12)

    def encode_docs(self, texts: list[str]) -> np.ndarray:
        return self._call(texts)

    def encode_query(self, text: str) -> np.ndarray:
        return self._call([text])[0]


_cache: Embedder | None = None


def get(force: bool = False) -> Embedder:
    """拿当前配置的嵌入器。进程内缓存 —— 加载 bge-m3 要几秒,不该每次查询都来一遍。"""
    global _cache
    if _cache is not None and not force:
        return _cache
    backend = (settings.embed_backend or "local").strip().lower()
    if backend == "dashscope":
        _cache = DashScopeEmbedder()
    elif backend == "local":
        _cache = LocalEmbedder(settings.embed_model)
    else:
        raise RuntimeError(f"EMBED_BACKEND 只能是 local / dashscope,拿到 {backend!r}")
    return _cache
